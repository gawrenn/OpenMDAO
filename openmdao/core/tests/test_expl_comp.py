"""Simple example demonstrating how to implement an explicit component."""
from __future__ import division

from six.moves import cStringIO

import unittest

from openmdao.api import Problem, Group, ExplicitComponent, IndepVarComp
from openmdao.devtools.testutil import assert_rel_error


class RectangleComp(ExplicitComponent):
    """
    A simple Explicit Component that computes the area of a rectangle.
    """
    def setup(self):
        self.add_input('length', val=1.)
        self.add_input('width', val=1.)
        self.add_output('area', val=1.)

    def compute(self, inputs, outputs):
        outputs['area'] = inputs['length'] * inputs['width']


class RectanglePartial(RectangleComp):

    def compute_partials(self, inputs, outputs, partials):
        partials['area', 'length'] = inputs['width']
        partials['area', 'width'] = inputs['length']


class RectangleJacVec(RectangleComp):

    def compute_jacvec_product(self, inputs, outputs,
                               d_inputs, d_outputs, mode):
        if mode == 'fwd':
            if 'area' in d_outputs:
                if 'length' in d_inputs:
                    d_outputs['area'] += inputs['width'] * d_inputs['length']
                if 'width' in d_inputs:
                    d_outputs['area'] += inputs['length'] * d_inputs['width']
        elif mode == 'rev':
            if 'area' in d_outputs:
                if 'length' in d_inputs:
                    d_inputs['length'] += inputs['width'] * d_outputs['area']
                if 'width' in d_inputs:
                    d_inputs['width'] += inputs['length'] * d_outputs['area']


class RectangleGroup(Group):

    def setup(self):
        comp1 = self.add_subsystem('comp1', IndepVarComp())
        comp1.add_output('length', 1.0)
        comp1.add_output('width', 1.0)

        self.add_subsystem('comp2', RectanglePartial())
        self.add_subsystem('comp3', RectangleJacVec())

        self.connect('comp1.length', 'comp2.length')
        self.connect('comp1.length', 'comp3.length')
        self.connect('comp1.width', 'comp2.width')
        self.connect('comp1.width', 'comp3.width')


class ExplCompTestCase(unittest.TestCase):

    def test_simple(self):
        prob = Problem(RectangleComp())
        prob.setup(check=False)
        prob.run_model()

    def test_compute(self):
        prob = Problem(RectangleGroup())
        prob.setup(check=False)

        prob['comp1.length'] = 3.
        prob['comp1.width'] = 2.
        prob.run_model()
        assert_rel_error(self, prob['comp2.area'], 6.)
        assert_rel_error(self, prob['comp3.area'], 6.)

        # total derivs
        total_derivs = prob.compute_total_derivs(
            wrt=['comp1.length', 'comp1.width'],
            of=['comp2.area', 'comp3.area']
        )
        assert_rel_error(self, total_derivs['comp2.area', 'comp1.length'], [[2.]])
        assert_rel_error(self, total_derivs['comp3.area', 'comp1.length'], [[2.]])
        assert_rel_error(self, total_derivs['comp2.area', 'comp1.width'], [[3.]])
        assert_rel_error(self, total_derivs['comp3.area', 'comp1.width'], [[3.]])

        # list inputs
        stream = cStringIO()
        inputs = prob.model.list_inputs(out_stream=stream)
        print(stream.getvalue())
        self.assertEqual(sorted(inputs), [
            ('comp2.length', [3.]),
            ('comp2.width',  [2.]),
            ('comp3.length', [3.]),
            ('comp3.width',  [2.]),
        ])

        # list explicit outputs
        stream = cStringIO()
        outputs = prob.model.list_outputs(implicit=False, out_stream=stream)
        print(stream.getvalue())
        self.assertEqual(sorted(outputs), [
            ('comp1.length', [3.]),
            ('comp1.width',  [2.]),
            ('comp2.area',   [6.]),
            ('comp3.area',   [6.]),
        ])

        # list states
        stream = cStringIO()
        states = prob.model.list_outputs(explicit=False, out_stream=stream)
        print(stream.getvalue())
        self.assertEqual(states, [])

        # list residuals
        stream = cStringIO()
        resids = prob.model.list_residuals(out_stream=stream)
        print(stream.getvalue())
        self.assertEqual(sorted(resids), [
            ('comp1.length', [0.]),
            ('comp1.width',  [0.]),
            ('comp2.area',   [0.]),
            ('comp3.area',   [0.]),
        ])


if __name__ == '__main__':
    unittest.main()
