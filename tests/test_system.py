#!/usr/bin/env python

import random
import copy
import unittest
from atooms.system import System
from atooms.system.cell import Cell
from atooms.system.particle import Particle

class Test(unittest.TestCase):

    def setUp(self):
        N = 100
        L = 10.0
        self.ref = System()
        self.ref.cell = Cell([L, L, L])
        self.ref.particle = []
        while len(self.ref.particle) <= N:
            pos = [(random.random()-0.5) * L,
                   (random.random()-0.5) * L,
                   (random.random()-0.5) * L]
            self.ref.particle.append(Particle(position=pos))

    def test_density(self):
        system = copy.copy(self.ref)
        density_old = system.density
        system.density = density_old*1.1
        self.assertAlmostEqual(system.density, density_old*1.3)

    def test_dump(self):
        self.assertAlmostEqual(self.ref.dump('ids')[-1],
                               self.ref.dump('particle.id')[-1])
        self.assertAlmostEqual(self.ref.dump('pos')[-1][-1],
                               self.ref.dump('particle.position')[-1][-1])
        self.assertAlmostEqual(self.ref.dump('vel')[-1][-1],
                               self.ref.dump('particle.velocity')[-1][-1])

if __name__ == '__main__':
    unittest.main()


