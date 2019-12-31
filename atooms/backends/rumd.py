# This file is part of atooms
# Copyright 2010-2017, Daniele Coslovich

"""Simulation backend for RUMD (http://rumd.org/)."""

# This enables importing the top-level rumd package and still call
# this file rumd.py
from __future__ import absolute_import

import sys
import os
import numpy
import logging

import rumd
from rumdSimulation import rumdSimulation
from atooms.system.particle import Particle
from atooms.system.cell import Cell
from atooms.core.utils import mkdir

_log = logging.getLogger(__name__)
_version = rumd.GetVersion()


class RUMD(object):

    """RUMD simulation backend."""

    version = _version

    def __init__(self, input_file_or_sim, forcefield_file=None,
                 forcefield=None, integrator=None, temperature=None,
                 dt=0.001, output_path=None, fixcm_interval=0):
        self.output_path = output_path
        # Keep a reference of the Trajectory backend class
        self.trajectory = Trajectory

        # Store internal rumd simulation instance.
        # It is exposed as self.rumd_simulation for further customization
        if isinstance(input_file_or_sim, rumdSimulation):
            self.rumd_simulation = input_file_or_sim
            self._suppress_all_output = False
            self._initialize_output = True

        else:
            self.rumd_simulation = rumdSimulation(input_file_or_sim, verbose=False)
            self.rumd_simulation.SetVerbose(False)
            self.rumd_simulation.sample.SetVerbose(False)
            self.rumd_simulation.sample.EnableBackup(False)
            self.rumd_simulation.SetMomentumResetInterval(fixcm_interval)
            self.rumd_simulation.SetBlockSize(sys.maxsize)
            self.rumd_simulation.write_timing_info = False

            # By default we mute RUMD output.
            if self.output_path is not None:
                mkdir(self.output_path)
                self.rumd_simulation.sample.SetOutputDirectory(self.output_path + '/rumd')
            self.rumd_simulation.SetOutputScheduling("energies", "none")
            self.rumd_simulation.SetOutputScheduling("trajectory", "none")
            self._suppress_all_output = True
            self._initialize_output = False

            # Set the forcefield
            if forcefield is not None:
                # We are provided a list of rumd potentials
                for potential in forcefield:
                    self.rumd_simulation.AddPotential(potential)
            # We parse the forcefield file
            # It should provide a list of potentials named potential
            if forcefield_file is not None:
                with open(forcefield_file) as fh:
                    exec(fh.read())
                if 'potential' not in locals():
                    raise ValueError('forcefield file should contain a list of potentials named potential')
                for pot in potential:
                    self.rumd_simulation.AddPotential(pot)
                
            # Add a rumd integrator
            if temperature is not None:
                integrator = 'nvt'

            if integrator is not None:
                if integrator in ['nvt', 'NVT']:
                    itg = rumd.IntegratorNVT(targetTemperature=temperature,
                                             timeStep=dt)
                elif integrator in ['nve', 'NVE']:
                    itg = rumd.IntegratorNVE(timeStep=dt)
                self.rumd_simulation.SetIntegrator(itg)

        # Copy of initial state
        self._initial_sample = self.rumd_simulation.sample.Copy()

        # Hold a reference to the system
        # self.system = System(self.rumd_simulation.sample, self.rumd_simulation.potentialList)
        
    # Wrapping system is needed because rumd holds a reference to the
    # potentials in rumd_simulation and they are needed to create a
    # working sample from scratch
    def _get_system(self):
        forcefield = self.rumd_simulation.potentialList
        return System(self.rumd_simulation.sample, forcefield)

    def _set_system(self, value):
        # Setting sample this way is useless.
        #   self.rumd_simulation.sample = value.sample
        # Rumd actually sets samples via a file, there seems to be no other way.
        # TODO: to retain modifications to system, use atooms trajectory but at the moment we would loose info on thermostat
        import tempfile
        from atooms.core.utils import rmd
        # Why should we set the output dir? It should not change
        #tmp = value.sample.GetOutputDirectory()
        dirout = tempfile.mkdtemp()
        file_tmp = os.path.join(dirout, 'sample.xyz.gz')
        value.sample.WriteConf(file_tmp)
        self.rumd_simulation.sample.ReadConf(file_tmp)
        # Why should we set the output dir? It should not change
        # value.sample.SetOutputDirectory(tmp)
        # Clean up
        rmd(dirout)
        
    system = property(_get_system, _set_system, 'System')

    def __str__(self):
        return 'RUMD'

    @property
    def rmsd(self):
        """
        Compute the mean square displacement between actual sample and the
        reference sample.
        """
        if self.rumd_simulation.sample is self._initial_sample:
            raise Exception('rmsd between two references of the same system does not make sense (use deepecopy?)')
        ndim = 3  # hard coded
        N = self.rumd_simulation.sample.GetNumberOfParticles()
        L = [self.rumd_simulation.sample.GetSimulationBox().GetLength(i) for i in range(ndim)]
        # Unfold positions using periodic image information
        ref = self._initial_sample.GetPositions() + self._initial_sample.GetImages() * L
        unf = self.rumd_simulation.sample.GetPositions() + self.rumd_simulation.sample.GetImages() * L
        return (sum(sum((unf - ref)**2)) / N)**0.5

    def write_checkpoint(self):
        if self.output_path is None:
            _log.warn('output_path is not set so we cannot write checkpoint')
        else:
            with Trajectory(self.output_path + '.chk', 'w') as t:
                t.write(self.system, None)

    def read_checkpoint(self):
        if os.path.exists(self.output_path + '.chk'):
            self.rumd_simulation.sample.ReadConf(self.output_path + '.chk')
        else:
            _log.debug('could not find checkpoint')

    def run(self, steps):
        self.rumd_simulation.Run(steps,
                                 suppressAllOutput=self._suppress_all_output,
                                 initializeOutput=self._initialize_output)
        self._initialize_output = False

class Thermostat(object):

    """Wrap a RUMD integrator as a thermostat."""

    # TODO: problem, RUMD must keep the same order in future versions
    # We should unit test this
    # Info string looks like IntegratorNVT,0.004,0.3602,0.2,-0.7223

    def __init__(self, integrator):
        self._integrator = integrator

    def reset(self):
        info = self._integrator.GetInfoString(18).split(',')
        info[4] = '1.0'
        info = ','.join(info)
        self._integrator.InitializeFromInfoString(info)

    def _get_temperature(self):
        info = self._integrator.GetInfoString(18).split(',')
        return float(info[2])

    def _set_temperature(self, value):
        info = self._integrator.GetInfoString(18).split(',')
        info[2] = '%g' % value
        info = ','.join(info)
        self._integrator.InitializeFromInfoString(info)

    temperature = property(_get_temperature, _set_temperature, 'Temperature')


class System(object):

    """System wrapper for RUMD."""

    def __init__(self, sample, forcefield=None):
        self.sample = sample
        self.forcefield = forcefield
        self.thermostat = Thermostat(self.sample.GetIntegrator())
        self.barostat = None
        self.reservoir = None

    def __copy__(self):
        # This is not really needed, it's just there for reference
        cls = self.__class__
        result = cls.__new__(cls)
        result.__dict__.update(self.__dict__)
        result.sample = self.sample.Copy()
        if self.forcefield is not None:
            for potential in self.forcefield:
                result.sample.SetPotential(potential)
        else:
            print 'cannot copy potentials'
        return result

    def __deepcopy__(self, memo):
        cls = self.__class__
        result = cls.__new__(cls)
        memo[id(self)] = result
        result.__dict__.update(self.__dict__)
        result.sample = self.sample.Copy()
        if self.forcefield is not None:
            for potential in self.forcefield:
                result.sample.SetPotential(potential)
        else:
            print 'cannot copy potentials' 
        return result

    def potential_energy(self, per_particle=False, normed=False, cache=False):
        self.sample.CalcF()
        if normed or per_particle:
            return self.sample.GetPotentialEnergy() / len(self.particle)
        else:
            return self.sample.GetPotentialEnergy()

    def kinetic_energy(self,per_particle=False, normed=False):
        # TODO: use double IntegratorNVT::GetKineticEnergy(bool copy) const{
        ekin = sum([p.kinetic_energy for p in self.particle])
        if normed or per_particle:
            return ekin / len(self.particle)
        else:
            return ekin

    def total_energy(self, per_particle=False, normed=False, cache=False):
        return self.potential_energy(per_particle=per_particle, normed=normed, cache=cache) +\
            self.kinetic_energy(per_particle=per_particle, normed=normed)

    def __get_mass(self):
        # TODO: cache it (but what if masses change?)
        npart = self.sample.GetNumberOfParticles()
        nsp = self.sample.GetNumberOfTypes()
        mass = numpy.ndarray(npart, dtype=float)
        ii = 0
        for i in range(nsp):
            ni = self.sample.GetNumberThisType(i)
            try:
                # This will work with rumd <= 2.0.1 I think
                # meta = self.sample.GetTrajectoryConfMetaData()
                # then get meta.GetMassOfType(i)
                mi = self.sample.GetMass(i)
            except:
                _log.warn('cannot get mass from RUMD interface, setting to 1.0')
                mi = 1.0
            mass[ii: ii + ni] = mi
            ii += ni
        return mass

    @property
    def temperature(self):
        ndof = self.sample.GetNumberOfDOFs()
        vel = self.sample.GetVelocities()
        mass = self.__get_mass()
        return numpy.sum(mass * numpy.sum(vel**2.0, 1)) / ndof

    def set_temperature(self, T):
        # Scale velocities from temperature Told to T
        # TODO: use maxwellian
        # TODO: remove CM velocity
        Told = self.temperature
        velocity_factor = (T/Told)**0.5
        self.sample.ScaleVelocities(velocity_factor)

    @property
    def cell(self):
        box = self.sample.GetSimulationBox()
        L = [box.GetLength(i) for i in range(3)]
        return Cell(L)

    @property
    def particle(self):
        # Warning: this is read only. If you change the particles, the
        # modification won't be propoagated to the RUMD objects.
        # One would have to create a new system.
        npart = self.sample.GetNumberOfParticles()
        pos = self.sample.GetPositions()
        vel = self.sample.GetVelocities()
        nsp = self.sample.GetNumberOfTypes()
        ima = self.sample.GetImages()
        mass = self.__get_mass()
        spe = numpy.ndarray(npart, dtype=int)
        ii = 0
        for i in range(nsp):
            ni = self.sample.GetNumberThisType(i)
            spe[ii: ii + ni] = i
            ii += ni
        p = [Particle(species=spe_i, mass=mass_i, position=pos_i,
                      velocity=vel_i) for spe_i, mass_i, pos_i, vel_i
             in zip(spe, mass, pos, vel)]
        for pi, i in zip(p, ima):
            pi.periodic_image = i
        return p

    def dump(self, what):
        import atooms.system
        system = atooms.system.System(self.particle, self.cell)
        return system.dump(what)
    
    def report(self):
        return ''


class Trajectory(object):

    suffix = 'xyz'

    def __init__(self, filename, mode='w'):
        self.filename = filename
        self.mode = mode

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()

    def write(self, system, step):
        """
        If step is not None, output will follow a folder-based logic and
        filename will be considered as the root folder
        """
        if step is None:
            f = self.filename
        else:
            fbase = '%011d.%s' % (step, self.suffix)
            f = os.path.join(self.filename, fbase)
            if not os.path.exists(self.filename):
                os.makedirs(self.filename)
        _log.debug('writing config via backend to %s at step %s, %s', f, step, self.mode)
        system.sample.WriteConf(f, self.mode)

    def close(self):
        # This only unzips files with no step info
        if os.path.exists(self.filename + '.gz'):
            os.system("gunzip -f %s.gz" % self.filename)
