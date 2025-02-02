# Support for duplication and mirroring modes for IDEX printers
#
# Copyright (C) 2021  Fabrice Gallet <tircown@gmail.com>
# Copyright (C) 2023  Dmitry Butyugin <dmbutyugin@google.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import math
import chelper

INACTIVE = 'INACTIVE'
PRIMARY = 'PRIMARY'
COPY = 'COPY'
MIRROR = 'MIRROR'

class DualCarriages:
    VALID_MODES = [PRIMARY, COPY, MIRROR]
    def __init__(self, dc_config, rail_0, rail_1, axis):
        self.printer = dc_config.get_printer()
        self.axis = axis
        self.dc = (rail_0, rail_1)
        self.saved_states = {}
        safe_dist = dc_config.getfloat('safe_distance', None, minval=0.)
        if safe_dist is None:
            dc0_rail = rail_0.get_rail()
            dc1_rail = rail_1.get_rail()
            safe_dist = min(abs(dc0_rail.position_min - dc1_rail.position_min),
                            abs(dc0_rail.position_max - dc1_rail.position_max))
        self.safe_dist = safe_dist
        self.printer.add_object('dual_carriage', self)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command(
                   'SET_DUAL_CARRIAGE', self.cmd_SET_DUAL_CARRIAGE,
                   desc=self.cmd_SET_DUAL_CARRIAGE_help)
        gcode.register_command(
                   'SAVE_DUAL_CARRIAGE_STATE',
                   self.cmd_SAVE_DUAL_CARRIAGE_STATE,
                   desc=self.cmd_SAVE_DUAL_CARRIAGE_STATE_help)
        gcode.register_command(
                   'RESTORE_DUAL_CARRIAGE_STATE',
                   self.cmd_RESTORE_DUAL_CARRIAGE_STATE,
                   desc=self.cmd_RESTORE_DUAL_CARRIAGE_STATE_help)
    def get_rails(self):
        return self.dc
    def toggle_active_dc_rail(self, index, override_rail=False):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.flush_step_generation()
        pos = toolhead.get_position()
        kin = toolhead.get_kinematics()
        for i, dc in enumerate(self.dc):
            dc_rail = dc.get_rail()
            if i != index:
                if dc.is_active():
                    dc.inactivate(pos)
                if override_rail:
                    kin.override_rail(3, dc_rail)
        target_dc = self.dc[index]
        if target_dc.mode != PRIMARY:
            newpos = pos[:self.axis] + [target_dc.get_axis_position(pos)] \
                        + pos[self.axis+1:]
            target_dc.activate(PRIMARY, newpos, old_position=pos)
            if override_rail:
                kin.override_rail(self.axis, target_dc.get_rail())
            toolhead.set_position(newpos)
        kin.update_limits(self.axis, target_dc.get_rail().get_range())
    def home(self, homing_state):
        kin = self.printer.lookup_object('toolhead').get_kinematics()
        for i, dc_rail in enumerate(self.dc):
            self.toggle_active_dc_rail(i, override_rail=True)
            kin.home_axis(homing_state, self.axis, dc_rail.get_rail())
        # Restore the original rails ordering
        self.toggle_active_dc_rail(0, override_rail=True)
    def get_status(self, eventtime=None):
        return {('carriage_%d' % (i,)) : dc.mode
                for (i, dc) in enumerate(self.dc)}
    def get_kin_range(self, toolhead, mode):
        pos = toolhead.get_position()
        axes_pos = [dc.get_axis_position(pos) for dc in self.dc]
        dc0_rail = self.dc[0].get_rail()
        dc1_rail = self.dc[1].get_rail()
        range_min = dc0_rail.position_min
        range_max = dc0_rail.position_max
        safe_dist = self.safe_dist

        if mode == COPY:
            range_min = max(range_min,
                            axes_pos[0] - axes_pos[1] + dc1_rail.position_min)
            range_max = min(range_max,
                            axes_pos[0] - axes_pos[1] + dc1_rail.position_max)
        elif mode == MIRROR:
            if dc0_rail.get_homing_info().positive_dir:
                range_min = max(range_min,
                                0.5 * (sum(axes_pos) + safe_dist))
                range_max = min(range_max,
                                sum(axes_pos) - dc1_rail.position_min)
            else:
                range_max = min(range_max,
                                0.5 * (sum(axes_pos) - safe_dist))
                range_min = max(range_min,
                                sum(axes_pos) - dc1_rail.position_max)
        else:
            # mode == PRIMARY
            active_idx = 1 if self.dc[1].is_active() else 0
            inactive_idx = 1 - active_idx
            if active_idx:
                range_min = dc1_rail.position_min
                range_max = dc1_rail.position_max
            if self.dc[active_idx].get_rail().get_homing_info().positive_dir:
                range_min = max(range_min, axes_pos[inactive_idx] + safe_dist)
            else:
                range_max = min(range_max, axes_pos[inactive_idx] - safe_dist)
        return (range_min, range_max)
    def activate_dc_mode(self, index, mode):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.flush_step_generation()
        kin = toolhead.get_kinematics()
        if mode == INACTIVE:
            self.dc[index].inactivate(toolhead.get_position())
        elif mode == PRIMARY:
            self.toggle_active_dc_rail(index)
        else:
            self.toggle_active_dc_rail(0)
            self.dc[index].activate(mode, toolhead.get_position())
        kin.update_limits(self.axis, self.get_kin_range(toolhead, mode))
    def _handle_ready(self):
        # Apply the transform later during Klipper initialization to make sure
        # that input shaping can pick up the correct stepper kinematic flags.
        for dc in self.dc:
            dc.apply_transform()
    cmd_SET_DUAL_CARRIAGE_help = "Configure the dual carriages mode"
    def cmd_SET_DUAL_CARRIAGE(self, gcmd):
        index = gcmd.get_int('CARRIAGE', minval=0, maxval=1)
        mode = gcmd.get('MODE', PRIMARY).upper()
        if mode not in self.VALID_MODES:
            raise gcmd.error("Invalid mode=%s specified" % (mode,))
        if mode in [COPY, MIRROR]:
            if index == 0:
                raise gcmd.error(
                        "Mode=%s is not supported for carriage=0" % (mode,))
            curtime = self.printer.get_reactor().monotonic()
            kin = self.printer.lookup_object('toolhead').get_kinematics()
            axis = 'xyz'[self.axis]
            if axis not in kin.get_status(curtime)['homed_axes']:
                raise gcmd.error(
                        "Axis %s must be homed prior to enabling mode=%s" %
                        (axis, mode))
        self.activate_dc_mode(index, mode)
    cmd_SAVE_DUAL_CARRIAGE_STATE_help = \
            "Save dual carriages modes and positions"
    def cmd_SAVE_DUAL_CARRIAGE_STATE(self, gcmd):
        state_name = gcmd.get('NAME', 'default')
        pos = self.printer.lookup_object('toolhead').get_position()
        self.saved_states[state_name] = {
            'carriage_modes': [dc.mode for dc in self.dc],
            'axes_positions': [dc.get_axis_position(pos) for dc in self.dc],
        }
    cmd_RESTORE_DUAL_CARRIAGE_STATE_help = \
            "Restore dual carriages modes and positions"
    def cmd_RESTORE_DUAL_CARRIAGE_STATE(self, gcmd):
        state_name = gcmd.get('NAME', 'default')
        saved_state = self.saved_states.get(state_name)
        if saved_state is None:
            raise gcmd.error("Unknown DUAL_CARRIAGE state: %s" % (state_name,))
        move_speed = gcmd.get('MOVE_SPEED', 0., above=0.)
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.flush_step_generation()
        pos = toolhead.get_position()
        if gcmd.get_int('MOVE', 1):
            for i, dc in enumerate(self.dc):
                self.toggle_active_dc_rail(i)
                saved_pos = saved_state['axes_positions'][i]
                toolhead.manual_move(
                        pos[:self.axis] + [saved_pos] + pos[self.axis+1:],
                        move_speed or dc.get_rail().homing_speed)
        for i, dc in enumerate(self.dc):
            saved_mode = saved_state['carriage_modes'][i]
            self.activate_dc_mode(i, saved_mode)

class DualCarriagesRail:
    ENC_AXES = [b'x', b'y']
    def __init__(self, rail, axis, active):
        self.rail = rail
        self.axis = axis
        self.mode = (INACTIVE, PRIMARY)[active]
        self.offset = 0.
        self.scale = 1. if active else 0.
        ffi_main, ffi_lib = chelper.get_ffi()
        self.dc_stepper_kinematics = []
        self.orig_stepper_kinematics = []
        for s in rail.get_steppers():
            sk = ffi_main.gc(ffi_lib.dual_carriage_alloc(), ffi_lib.free)
            orig_sk = s.get_stepper_kinematics()
            ffi_lib.dual_carriage_set_sk(sk, orig_sk)
            # Set the default transform for the other axis
            ffi_lib.dual_carriage_set_transform(
                    sk, self.ENC_AXES[1 - axis], 1., 0.)
            self.dc_stepper_kinematics.append(sk)
            self.orig_stepper_kinematics.append(orig_sk)
            s.set_stepper_kinematics(sk)
    def get_rail(self):
        return self.rail
    def is_active(self):
        return self.mode != INACTIVE
    def get_axis_position(self, position):
        return position[self.axis] * self.scale + self.offset
    def apply_transform(self):
        ffi_main, ffi_lib = chelper.get_ffi()
        for sk in self.dc_stepper_kinematics:
            ffi_lib.dual_carriage_set_transform(
                    sk, self.ENC_AXES[self.axis], self.scale, self.offset)
    def activate(self, mode, position, old_position=None):
        old_axis_position = self.get_axis_position(old_position or position)
        self.scale = -1. if mode == MIRROR else 1.
        self.offset = old_axis_position - position[self.axis] * self.scale
        self.apply_transform()
        self.mode = mode
    def inactivate(self, position):
        self.offset = self.get_axis_position(position)
        self.scale = 0.
        self.apply_transform()
        self.mode = INACTIVE
