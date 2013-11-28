from __future__ import division

__copyright__ = "Copyright (C) 2013 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""


import numpy as np
#import numpy.linalg as la
import modepy as mp
from pytools import memoize_method
from pytential.discretization.poly_element import (
        PolynomialElementGroupBase,
        PolynomialElementDiscretizationBase,
        PolynomialElementDiscretization)

import pyopencl as cl

__doc__ = """
.. autoclass:: QBXDiscretization
"""


# {{{ jump term interface helper

class _JumpTermArgumentProvider(object):
    def __init__(self, discr, density, ds_direction, side=None):
        self.discr = discr
        self.density = density
        self.ds_direction = ds_direction
        self.side = side

    @property
    def normal(self):
        return self.discr.curve.normals.reshape(2, -1).T

    @property
    def tangent(self):
        return self.discr.curve.tangents.reshape(2, -1).T

    @property
    def src_derivative_dir(self):
        return self.ds_direction

    @property
    def mean_curvature(self):
        return self.discr.curve.curvature.reshape(-1)

    @property
    def density_0(self):
        return self.density.reshape(-1)

    @property
    @memoize_method
    def density_0_prime(self):
        diff_mat = self.discr.curve.expansion.get_differentiation_matrix()
        return (2 * np.dot(diff_mat, self.density.T).T.reshape(-1)
                / self.discr.curve.speed.reshape(-1))

# }}}


# {{{ element group

class QBXElementGroup(PolynomialElementGroupBase):
    @memoize_method
    def _quadrature_rule(self):
        dims = self.mesh_el_group.dim
        if dims == 1:
            return mp.LegendreGaussQuadrature(self.order)
        else:
            return mp.XiaoGimbutasSimplexQuadrature(self.order, dims)

    @property
    @memoize_method
    def unit_nodes(self):
        return self._quadrature_rule().nodes

    @property
    @memoize_method
    def weights(self):
        return self._quadrature_rule().weights

# }}}


# {{{ QBX discretization

class QBXDiscretization(PolynomialElementDiscretizationBase):
    """An (unstructured) grid for discretizing a QBX operator.

    .. attribute :: mesh
    .. attribute :: groups
    .. attribute :: nnodes

    .. autoattribute :: nodes

    See :ref:`qbxguts` for some information on the inner workings of this.
    """
    def __init__(self, cl_ctx, mesh, exact_order, qbx_order,
            target_discr,
            expansion_getter=None, real_dtype=np.float64):
        """
        :arg exact_order: The total degree to which the underlying quadrature
            is exact.
        """

        PolynomialElementDiscretizationBase.__init__(
                self, cl_ctx, mesh, exact_order, real_dtype)

        self.qbx_order = qbx_order
        self.target_discr = target_discr

        if expansion_getter is None:
            from sumpy.expansion.local import LineTaylorLocalExpansion
            expansion_getter = LineTaylorLocalExpansion
        self.expansion_getter = expansion_getter

    group_class = QBXElementGroup

    @memoize_method
    def centers(self, target_discr, sign):
        from pytential import sym, bind
        with cl.CommandQueue(self.cl_context) as queue:
            return bind(target_discr,
                    sym.Nodes() + 2*sign*sym.area_element()*sym.normal())(queue) \
                            .as_vector(np.object)

    @memoize_method
    def weights_and_area_elements(self):
        import pytential.symbolic.primitives as p
        from pytential.symbolic.execution import bind
        with cl.CommandQueue(self.cl_context) as queue:
            return bind(self,
                    p.area_element() * p.QWeight())(queue)

    # {{{ interface with execution

    def preprocess_optemplate(self, name, discretizations, expr):
        """
        :arg name: The symbolic name for *self*, which the preprocessor
            should use to find which expressions it is allowed to modify.
        """
        from pytential.symbolic.mappers import QBXPreprocessor
        return QBXPreprocessor(name, discretizations)(expr)

    def op_group_features(self, expr):
        from pytential.symbolic.primitives import IntGdSource
        assert not isinstance(expr, IntGdSource)

        from sumpy.kernel import AxisTargetDerivativeRemover
        result = (expr.source, expr.target, expr.density,
                AxisTargetDerivativeRemover()(expr.kernel))

        return result

    def exec_layer_potential_insn(self, queue, insn, bound_expr, evaluate):
        return self.exec_layer_potential_insn_direct(
                queue, insn, bound_expr, evaluate)

    # {{{ fmm-based execution

    @property
    @memoize_method
    def qbx_fmm_code_getter(self):
        from pytential.discretization.qbx.geometry import QBXFMMGeometryCodeGetter
        return QBXFMMGeometryCodeGetter(self.cl_context, self.ambient_dim)

    @memoize_method
    def qbx_fmm_geometry_data(self, target_discrs_and_qbx_sides):
        """
        :arg target_discrs_and_qbx_sides:
            a tuple of *(discr, qbx_forced_limit)*
            tuples, where *discr* is a
            :class:`pytential.discretization.Discretization`
            or
            :class:`pytential.discretization.target.TargetBase`
            instance
        """
        from pytential.discretization.qbx.geometry import QBXFMMGeometryData

        # FIXME set debug=False once everything works
        return QBXFMMGeometryData(self.qbx_fmm_code_getter,
                self, target_discrs_and_qbx_sides, debug=True)

    @memoize_method
    def expansion_wrangler_code_container(self, base_kernel, out_kernels):
        from sumpy.expansion.multipole import VolumeTaylorMultipoleExpansion
        from sumpy.expansion.local import VolumeTaylorLocalExpansion

        # FIXME: Don't hard-code FMM order
        fmm_order = self.qbx_order

        # FIXME: Don't hard-code expansion types
        fmm_mpole_expn = VolumeTaylorMultipoleExpansion(base_kernel, fmm_order)
        fmm_local_expn = VolumeTaylorLocalExpansion(base_kernel, fmm_order)
        qbx_local_expn = VolumeTaylorLocalExpansion(
                base_kernel, self.qbx_order)

        from pytential.discretization.qbx.fmm import \
                QBXExpansionWranglerCodeContainer
        return QBXExpansionWranglerCodeContainer(
                self.cl_context, fmm_mpole_expn, fmm_local_expn, qbx_local_expn,
                out_kernels)

    def exec_layer_potential_insn_fmm(self, queue, insn, bound_expr, evaluate):
        # {{{ build list of unique target discretizations used

        # map (name, qbx_side) to number in list
        tgt_name_and_side_to_number = {}
        # list of tuples (discr, qbx_side)
        target_discrs_and_qbx_sides = []

        for o in insn.outputs:
            key = (o.target_name, o.qbx_forced_limit)
            if key not in tgt_name_and_side_to_number:
                tgt_name_and_side_to_number[key] = \
                        len(target_discrs_and_qbx_sides)
                target_discrs_and_qbx_sides.append(
                        (bound_expr.discretizations[o.target_name],
                            o.qbx_forced_limit))

        target_discrs_and_qbx_sides = tuple(target_discrs_and_qbx_sides)

        # }}}

        geo_data = self.qbx_fmm_geometry_data(target_discrs_and_qbx_sides)

        # FIXME Exert more positive control over geo_data attribute lifetimes using
        # geo_data.<method>.clear_cache(geo_data).

        # FIXME Synthesize "bad centers" around corners and edges that have
        # inadequate QBX coverage.

        # FIXME don't compute *all* output kernels on all targets--respect that
        # some target discretizations may only be asking for derivatives (e.g.)

        strengths = (evaluate(insn.density).with_queue(queue)
                * self.weights_and_area_elements())

        # {{{ get expansion wrangler

        base_kernel = None
        out_kernels = []

        from sumpy.kernel import AxisTargetDerivativeRemover
        for knl in insn.kernels:
            candidate_base_kernel = AxisTargetDerivativeRemover()(knl)

            if base_kernel is None:
                base_kernel = candidate_base_kernel
            else:
                assert base_kernel == candidate_base_kernel

        out_kernels = tuple(knl for knl in insn.kernels)

        if base_kernel.is_complex_valued:
            value_dtype = self.complex_dtype
        else:
            value_dtype = self.real_dtype

        # FIXME: extra_kwargs
        wrangler = self.expansion_wrangler_code_container(
                base_kernel, out_kernels).get_wrangler(
                        queue, geo_data, value_dtype)

        # }}}

        #geo_data.global_qbx_centers_box_target_lists()
        #geo_data.non_qbx_box_target_lists()
        #geo_data.global_qbx_centers_to_targets()

        #geo_data.plot()

        if len(geo_data.global_qbx_centers()) != geo_data.center_info().ncenters:
            raise NotImplementedError("geometry has centers requiring local QBX")

        from pytential.discretization.qbx.geometry import target_state
        if (geo_data.target_to_center().with_queue(queue)
                == target_state.FAILED).get().any():
            raise RuntimeError("geometry has failed targets")

        # {{{ execute global QBX

        from pytential.discretization.qbx.fmm import drive_fmm
        all_potentials_on_every_tgt = drive_fmm(wrangler, strengths)

        # }}}

        result = []

        for o in insn.outputs:
            tgt_side_number = tgt_name_and_side_to_number[
                    o.target_name, o.qbx_forced_limit]
            tgt_slice = slice(*geo_data.target_info().target_discr_starts[
                    tgt_side_number:tgt_side_number+2])

            result.append(
                    (o.name,
                        all_potentials_on_every_tgt[o.kernel_index][tgt_slice]))

        return result, []

    # }}}

    # {{{ direct execution

    @memoize_method
    def get_lpot_applier_and_arg_names_to_exprs(self, kernels):
        # needs to be separate method for caching

        from pytools import any
        if any(knl.is_complex_valued for knl in kernels):
            value_dtype = self.complex_dtype
        else:
            value_dtype = self.real_dtype

        from sumpy.qbx import LayerPotential
        lpot_applier = LayerPotential(self.cl_context,
                    [self.expansion_getter(knl, self.qbx_order)
                        for knl in kernels],
                    value_dtypes=value_dtype)

        from pytential.symbolic.mappers import KernelEvalArgumentCollector
        keac = KernelEvalArgumentCollector()
        arg_names_to_exprs = {}
        for k in kernels:
            arg_names_to_exprs.update(keac(k))

        return lpot_applier, arg_names_to_exprs

    @memoize_method
    def get_p2p(self, kernels):
        # needs to be separate method for caching

        from pytools import any
        if any(knl.is_complex_valued for knl in kernels):
            value_dtype = self.complex_dtype
        else:
            value_dtype = self.real_dtype

        from sumpy.p2p import P2P
        p2p = P2P(self.cl_context,
                    kernels, exclude_self=False, value_dtypes=value_dtype)

        return p2p

    def exec_layer_potential_insn_direct(self, queue, insn, bound_expr, evaluate):
        lp_applier, arg_names_to_exprs = \
                self.get_lpot_applier_and_arg_names_to_exprs(insn.kernels)
        p2p = None

        kernel_args = {}
        for arg_name, arg_expr in arg_names_to_exprs.iteritems():
            kernel_args[arg_name] = evaluate(arg_expr)

        from pymbolic import var
        from sumpy.tools import gather_arguments
        kernel_args.update(
                (arg.name, evaluate(var(arg.name)))
                for arg in gather_arguments(lp_applier.kernels)
                if arg.name not in kernel_args)

        strengths = (evaluate(insn.density).with_queue(queue)
                * self.weights_and_area_elements())

        # FIXME: Do this all at once
        result = []
        for o in insn.outputs:
            target_discr = bound_expr.discretizations[o.target_name]

            is_self = self is target_discr
            if not is_self:
                try:
                    is_self = target_discr.source_discr is self
                except AttributeError:
                    # apparently not.
                    pass

            if is_self:
                # QBXPreprocessor is supposed to have taken care of this
                assert o.qbx_forced_limit is not None
                assert abs(o.qbx_forced_limit) > 0

                evt, output_for_each_kernel = lp_applier(queue, target_discr.nodes(),
                        self.nodes(),
                        self.centers(target_discr, o.qbx_forced_limit),
                        [strengths], **kernel_args)
                result.append((o.name, output_for_each_kernel[o.kernel_index]))
            else:
                # yuck, no caching
                if p2p is None:
                    p2p = self.get_p2p(insn.kernels)
                evt, output_for_each_kernel = p2p(queue,
                        target_discr.nodes(), self.nodes(),
                        [strengths], **kernel_args)
                result.append((o.name, output_for_each_kernel[o.kernel_index]))

        return result, []

    # }}}

    # }}}

# }}}


def make_upsampling_qbx_discr(cl_ctx, mesh, target_order, qbx_order,
        source_order=None, expansion_getter=None, real_dtype=np.float64):
    if source_order is None:
        # twice as many points in 1D?
        source_order = target_order * 4

    tgt_discr = PolynomialElementDiscretization(
            cl_ctx, mesh, target_order, real_dtype=real_dtype)
    src_discr = QBXDiscretization(
            cl_ctx, mesh, source_order, qbx_order,
            tgt_discr,
            expansion_getter=expansion_getter, real_dtype=real_dtype)

    from pytential.discretization.upsampling import \
            UpsampleToSourceDiscretization
    return UpsampleToSourceDiscretization(tgt_discr, src_discr)


# vim: fdm=marker
