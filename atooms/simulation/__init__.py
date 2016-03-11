# This file is part of atooms
# Copyright 2010-2014, Daniele Coslovich

"""Simulation base class handling callbacks logic"""

import sys
import os
import time
import datetime
import logging
import copy

from atooms import __version__
from atooms.utils import mkdir
from atooms.utils import rank, size

# Logging facilities

class ParallelFilter(logging.Filter):
    def filter(self, rec):
        if hasattr(rec, 'rank'):
            if rec.rank == 'all':
                return True
            else:
                return rank == rec.rank
        else:
            return rank == 0

class MyFormatter(logging.Formatter):
    def format(self, record):
        if record.levelname in ['WARNING', 'ERROR']:
            return '# %s: %s' % (record.levelname, record.msg)
        else:
            return '# %s' % record.msg

log = logging.getLogger('atooms')
formatter = MyFormatter()
handler = logging.StreamHandler(stream=sys.stdout)
handler.setFormatter(formatter)
log.addFilter(ParallelFilter())
log.addHandler(handler)
log.setLevel(logging.INFO)

# Default exceptions

class SimulationEnd(Exception):
    pass

class WallTimeLimit(Exception):
    pass

class SchedulerError(Exception):
    pass

class ParameterError(Exception):
    pass

# Default observers

# Different approaches are possible:
# 1. use callable classes passing args to __init__ and interval to add()
#    Exemple of client code:
#      sim.add(simulation.WriterThermo(), interval=100)
#      sim.add(simulation.TargetRMSD(5.0))

# 2. use functions passing interval and args as kwargs 
#    args are then passed to the function upon calling
#    In this case, we should differentiate types of callbacks via different add() methods
#    Exemple of client code:
#      sim.add(simulation.writer_thermo, interval=100)
#      sim.add(simulation.target_rmsd, rmsd=5.0)

# At least for default observers, it should be possible to use shortcuts
#   sim.writer_thermo_interval = 100
#   sim.target_rmsd = 5.0
# which will then add / modify the callbacks

# We identify callbacks by some types
# * target : these callbacks raise a SimualtionEnd when it's over
# * writer : these callbacks dump useful stuff to file
# and of course general purpose callback can be passed to do whatever

# It os the backend's responsibility to implement specific Writer observers.

# TODO: several related todos on where to store output file paths. It
# would make sense to keep them in Writers and delegate to them
# (instead of having them in simulation objects). This would require being able
# to access writers more directly.

class WriterThermo(object):

    def __str__(self):
        return 'thermo'

    def __call__(self, e):
        pass

class WriterConfig(object):

    def __str__(self):
        return 'config'

    def __call__(self, e):
        pass

class WriterCheckpoint(object):

    def __str__(self):
        return 'checkpoint'

    def __call__(self, e):
        pass

def sec2time(t):
    eta_d = t / (24.0*3600)
    eta_h = (eta_d - int(eta_d)) * 24
    eta_m = (eta_h - int(eta_h)) * 60.0
    eta_s = (eta_m - int(eta_m)) * 60.0
    return '%dd:%02dh:%02dm:%02ds' % (eta_d, eta_h, eta_m, eta_s)


class Speedometer(object):

    def __init__(self, what=None):
        self._init = False
        self._what = what

    def __str__(self):
        return 'speedometer'

    def __call__(self, e):
        if not self._init:
            # We could store all this in __init__() but this
            # way we allow targeters added to simulation via add()
            for c in e._callback:
                if isinstance(c, Target):
                    self.name_target = c.name
                    self.x_target = c.target
                    self.t_last = time.time()
                    self.x_last = float(getattr(e, self.name_target))
                    self._init = True
                    return

        if self.x_target > 0:
            # Get the speed at which the simulation advances
            t_now = time.time()
            x_now = float(getattr(e, self.name_target))
            speed = (x_now - self.x_last) / (t_now - self.t_last)
            # Report fraction of target achieved and ETA
            frac = float(x_now) / self.x_target
            eta = (self.x_target-x_now) / speed
            delta = sec2time(eta)
            if self._what == 'ETA':
                d_now = datetime.datetime.now()
                d_delta = datetime.timedelta(seconds=eta)
                d_eta = d_now + d_delta
                log.info('estimated end: %s TSP: %.2g' % (d_eta.strftime('%h %d %Y at %H:%M'),
                                                      e.wall_time_per_step_particle()))
            else:
                log.info('%s: %d%% ETA: %s TSP: %.2g' % (self.name_target,
                                                         int(frac * 100), delta,
                                                         e.wall_time_per_step_particle()))

        self.t_last = t_now
        self.x_last = x_now


class Target(object):

    def __init__(self, name, target):
        self.name = name
        self.target = target

    def __call__(self, sim):
        x = float(getattr(sim, self.name))        
        if self.target > 0:
            frac = float(x) / self.target
            log.debug('targeting %s to %g [%d]' % (self.name, x, int(frac * 100)))
        if x >= self.target:
            raise SimulationEnd('achieved target %s: %s' % (self.name, self.target))

    def fraction(self, sim):
        """Fraction of target value already achieved"""
        return float(getattr(sim, self.name)) / self.target

    def __str__(self):
        return self.name
        
class TargetSteps(Target):

    # Note: this class is there just as an insane proof of principle
    # Steps targeting can/should be implemented by checking a simple int variable

    def __init__(self, target):
        Target.__init__(self, 'steps', target)
        
class TargetRMSD(Target):

    def __init__(self, target):
        Target.__init__(self, 'rmsd', target)

class TargetWallTime(Target):

    def __init__(self, wall_time):
        self.wtime_limit = wall_time

    def __call__(self, sim):
        if sim.elapsed_wall_time() > self.wtime_limit:
            raise WallTimeLimit('target wall time reached')
        else:
            t = sim.elapsed_wall_time()
            dt = self.wtime_limit - t
            log.debug('elapsed time %g, reamining time %g' % (t, dt))

class UserStop(object):
    """Allows a user to stop the simulation smoothly by touching a STOP
    file in the output root directory.
    Currently the file is not deleted to allow parallel jobs to all exit.
    """
    def __call__(self, e):
        # To make it work in parallel we should broadcast and then rm 
        # or subclass userstop in classes that use parallel execution
        if e.output_path is not None and self.STORAGE == 'directory':
            log.debug('User Stop %s/STOP' % e.output_path)
            # TODO: support files as well
            if os.path.exists('%s/STOP' % e.output_path):
                raise SimulationEnd('user has stopped the simulation')
        else:
            raise RuntimeError('USerStop wont work atm with file storage')

#TODO: interval can be a function to allow non linear sampling
class Scheduler(object):

    """Scheduler to call observer during the simulation"""

    def __init__(self, interval, calls=None, target=None):
        self.interval = interval
        self.calls = calls
        self.target = target
        # Main switch
        if interval>0:
            # Fixed interval.
            self.interval = interval
        else:
            if calls>0:
                # Fixed number of calls.
                if self.target is not None:
                    # If both calls and target are not None, we determine interval
                    self.interval = max(1, self.target / self.calls)
                else:
                    # Dynamic scheduling
                    raise SchedulerError('dynamic scheduling not implemented')
            # else:
            #     # Scheduler is disabled
            #     self.interval = 0 #sys.maxint

    def next(self, this):
        if self.interval>0:
            return (this / self.interval + 1) * self.interval
        else:
            return sys.maxint

class OnetimeScheduler(object):

    """Scheduler to call observer during the simulation"""

    def __init__(self, interval):
        self.interval = interval
        self.calls = None
        self.target = None

    def next(self, this):
        if this / self.interval == 0:
            return self.interval
        else:
            return sys.maxint


class Simulation(object):

    """Simulation abstract class using callback support."""

    # TODO: write initial configuration as well

    # Comvoluted trick to allow subclass to use custom observers for
    # target and writer without overriding setup(): have class
    # variables to point to the default observer classes that may be
    # switched to custom ones in subclasses
    _TARGET_STEPS = TargetSteps
    _TARGET_RMSD = TargetRMSD
    _WRITER_THERMO = WriterThermo
    _WRITER_CONFIG = WriterConfig    
    _WRITER_CHECKPOINT = WriterCheckpoint

    # Storage class attribute: backends can either dump to a directory
    # or following a file suffix logic. Meant to be subclassed.
    STORAGE = 'directory'

    # TODO: initial state is not needed anymore, since rmsd is the subclass or backend responsability (it was a too weak link, the only thing for which initial_state and system were needed.
    def __init__(self, initial_state, output_path=None, 
                 steps=0, rmsd=None,
                 thermo_interval=0, thermo_number=0, 
                 config_interval=0, config_number=0,
                 checkpoint_interval=0, checkpoint_number=0,
                 fixcm_interval=0,
                 restart=False):
        """We expect input and output paths as input.
        Alternatively, input might be a system (or trajectory?) instance.
        """
        self._callback = []
        self._scheduler = []
        self.fixcm_interval = fixcm_interval
        self.restart = restart
        self.output_path = output_path # can be None, file or directory
        self.steps = 0
        self.initial_steps = 0
        self.start_time = time.time()
        if self.output_path is not None and self.STORAGE == 'directory':
            mkdir(self.output_path)
        # TODO: output_path shouldn't be changed after init
        # We expect subclasses to keep a ref to the trajectory object self.trajectory
        # used to store configurations, although this is not used in base class

        # Setup schedulers and callbacks
        self.target_steps = steps
        self.writer_thermo = self._WRITER_THERMO()
        self.writer_config = self._WRITER_CONFIG()
        self.writer_checkpoint = self._WRITER_CHECKPOINT()

        # Target steps are checked only at the end of the simulation
        self.add(self._TARGET_STEPS(self.target_steps), Scheduler(max(1, self.target_steps)))
        # TODO: rmsd targeting interval is hard coded
        # TODO: implement dynamic scheduling or fail when interval is None and targeting is rmsd
        if rmsd is not None:
            self.add(self._TARGET_RMSD(rmsd), Scheduler(10000))
        # Writers
        self.add(Speedometer(), Scheduler(None, calls=10, target=self.target_steps))
        self.add(Speedometer(what='ETA'), OnetimeScheduler(max(1, int(self.target_steps/200.))))
        self.add(self.writer_thermo, Scheduler(thermo_interval, thermo_number, self.target_steps))
        self.add(self.writer_config, Scheduler(config_interval, config_number, self.target_steps))
        self.add(self.writer_checkpoint, Scheduler(checkpoint_interval, checkpoint_number, self.target_steps))

    def setup(self, 
              target_steps=None, target_rmsd=None,
              thermo_period=None, thermo_number=None, 
              config_period=None, config_number=None,
              checkpoint_period=None, checkpoint_number=None,
              reset=False):
        raise RuntimeError('use of deprecated setup() function')

    def _check_writers(self):
        """Check that we have a place to write"""
        # It should be called at pre()
        # TODO: in this case we should disable writers or check that
        if self.output_path is None:
            # if not all(x is None for x in [config_interval, thermo_interval, checkpoint_interval,
            #                                config_number, thermo_number, checkpoint_number]):
            #     raise ParameterError('cannot set writers when output_path is None')
            pass

    @property
    def rmsd(self):
        log.warning('rmsd has not been subclassed')
        return 0.0
        # TODO: provide rmsd by species 07.12.2014

    def add(self, callback, scheduler):
        """Register an observer (callback) to be called along with a scheduler"""
        # There are certainly more elegant ways of sorting Writers < Checkpoint < Target 
        # but anyway...
        # Keep targeters last
        if not isinstance(callback, Target):
            self._callback.insert(0, callback)
            self._scheduler.insert(0, scheduler)
        else:
            self._callback.append(callback)
            self._scheduler.append(scheduler)

        # Enforce checkpoint is last among non_targeters
        try:
            idx = self._callback.index(self.writer_checkpoint)
            cnt = len(self._non_targeters)
            self._callback.insert(cnt-1, self._callback.pop(idx))
            self._scheduler.insert(cnt-1, self._scheduler.pop(idx))
        except ValueError:
            pass

    @property
    def _targeters(self):
        return [o for o in self._callback if isinstance(o, Target)]

    @property
    def _non_targeters(self):
        return [o for o in self._callback if not isinstance(o, Target)]

    def report(self):
        txt = '%s' % self
        nch = len(txt)
        log.info('')
        log.info(txt)
        log.info('')
        log.info('atooms version: %s' % __version__)
        self._report()
        self._report_observers()
        
    def _report(self):
        """Implemented by subclassed"""
        pass

    def _report_observers(self):
        for f, s in zip(self._callback, self._scheduler):
            if isinstance(f, Target):
                log.info('target %s: %s' % (f, f.target)) #, s.interval, s.calls))
            else:
                log.info('writer %s: interval=%s calls=%s' % (f, s.interval, s.calls))

    def notify(self, observers):
        for o in observers:
            o(self)
    
    def elapsed_wall_time(self):
        return time.time() - self.start_time

    def wall_time_per_step(self):
        """Return the wall time in seconds per step.
        Can be conventiently subclassed by more complex simulation classes."""
        return self.elapsed_wall_time() / (self.steps-self.initial_steps)

    def wall_time_per_step_particle(self):
        """Return the wall time in seconds per step and particle.
        Can be conventiently subclassed by more complex simulation classes."""
        return self.wall_time_per_step() / len(self.system.particle)

    # Our template consists of three steps: pre, until and end
    # Typically a backend will implement the until method.
    # It is recommended to *extend* (not override) the base run_pre() in subclasses
    # TODO: when should checkpoint be read? The logic must be set here
    # Having a read_checkpoint() stub method would be OK here.
    def run_pre(self):
        """This should be safe to called by subclasses before or after reading checkpoint"""
        pass
            
    def run_until(self, n):
        # Design: it is run_until responsability to set steps: self.steps = n
        # bear it in mind when subclassing 
        pass

    def _report_end(self):
        log.info('final rmsd: %.2f' % self.rmsd)
        log.info('wall time [s]: %.1f' % self.elapsed_wall_time())
        log.info('TSP [s/step/particle]: %.2f' % (self.wall_time_per_step_particle()))
        log.info('simulation ended on: %s' % datetime.datetime.now().strftime('%h %d %Y at %H:%M'))

    def run(self, target_steps=None):
        if target_steps is not None:
            # If we ask for more steps on the go, it means we are restarting
            # TODO: really, we might just want to run a new simulation...
            if target_steps > self.target_steps:
                self.restart = True
            self.target_steps = target_steps
            log.info('targeted number of steps: %s' % self.target_steps)

        self.initial_steps = copy.copy(self.steps)
        self.run_pre()
        self.report()

        try:
            # Before entering the simulation, check if we can quit right away
            # Then notify non targeters unless restarting
            self.notify(self._targeters)
            if not self.restart:
                self.notify(self._non_targeters)
            log.info('starting at step: %d' % self.steps)
            log.info('')
            while True:
                # Run simulation until any of the observers need to be called
                all_steps = [s.next(self.steps) for s in self._scheduler]
                next_step = min(all_steps)
                # Find observers indexes corresponding to minimum step
                # then get all corresponding observers
                next_step_ids = [i for i, step in enumerate(all_steps) if step == next_step]
                next_obs = [self._callback[i] for i in next_step_ids]
                self.run_until(next_step)
                self.steps = next_step
                # Observers are sorted such that targeters are last
                # and checkpoint is last among writers
                self.notify(next_obs)
                    
        except SimulationEnd as s:
            # TODO: make checkpoint is an explicit method of simulation and not a callback! 16.12.2014
            # The rationale here is that notify will basically check for now() but writecheckpoint must
            # here we must call no matter how the period fits. The problem was that PT checkpoint
            # was not a subclass of base one, and so it was not called!
            self.writer_checkpoint(self)
            # for f in self._callback:
            #     if isinstance(f, WriterCheckpoint):
            #         f(self)
            #self.notify(lambda x : isinstance(x, WriterCheckpoint))
            # Only dumps log if actually we did some steps
            log.info('%s' % s.message)
            if not self.initial_steps == self.steps:
                self._report_end()
            # TODO: hey! run_end is not called anymore!!
            # TODO: do we really need run_end() if write_checkpoint is called last and it does everything we won't need this...

        except KeyboardInterrupt:
            pass

        except:
            log.error('simulation failed')
            raise

        finally:
            pass
            log.info('goodbye')
