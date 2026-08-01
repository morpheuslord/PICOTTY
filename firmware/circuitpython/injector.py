# injector.py — the keystroke path toward the target, over USB HID.
#
# Two things happen here: literal text is typed with KeyboardLayoutUS.write (which
# takes a per-character `delay`, exactly what the protocol's char_delay_ms wants),
# and named chords like ["CONTROL","ALT","DELETE"] are mapped to Keycode values
# and sent with Keyboard.send (which presses then releases all).
#
# Keycode attribute names are the canonical UPPERCASE names from adafruit_hid
# (digits are ONE..NINE/ZERO, arrows are *_ARROW, ENTER/RETURN and SPACE/SPACEBAR
# are aliases). We add a small forgiving alias table so the hub can send common
# shorthands (CTRL, ESC, DEL, WIN, UP, PGUP, "5", ...) too.

import time

from adafruit_hid.keycode import Keycode

# Shorthands -> canonical Keycode attribute names. Applied before getattr.
_ALIASES = {
    "CTRL": "CONTROL",
    "CONTROL": "CONTROL",
    "ESC": "ESCAPE",
    "DEL": "DELETE",
    "WIN": "GUI",
    "SUPER": "GUI",
    "META": "GUI",
    "CMD": "COMMAND",
    "PGUP": "PAGE_UP",
    "PGDN": "PAGE_DOWN",
    "PGDOWN": "PAGE_DOWN",
    "UP": "UP_ARROW",
    "DOWN": "DOWN_ARROW",
    "LEFT": "LEFT_ARROW",
    "RIGHT": "RIGHT_ARROW",
    "PRINTSCREEN": "PRINT_SCREEN",
    "PRTSC": "PRINT_SCREEN",
    "MENU": "APPLICATION",
    "RETURN": "ENTER",
    "SPACE": "SPACEBAR",
    "0": "ZERO",
    "1": "ONE",
    "2": "TWO",
    "3": "THREE",
    "4": "FOUR",
    "5": "FIVE",
    "6": "SIX",
    "7": "SEVEN",
    "8": "EIGHT",
    "9": "NINE",
}

_MAX_KEYS = 6  # HID report holds 6 non-modifier keys; modifiers are separate.


class InjectError(Exception):
    """A command could not be executed as typed/sent."""


class Injector:
    def __init__(self, keyboard, layout, default_char_delay_ms=0):
        self._kbd = keyboard
        self._layout = layout
        self._default_delay_ms = default_char_delay_ms

    def _resolve(self, name):
        if not isinstance(name, str) or not name:
            raise InjectError("empty keycode name")
        key = name.strip().upper()
        key = _ALIASES.get(key, key)
        try:
            return getattr(Keycode, key)
        except AttributeError:
            raise InjectError("unknown keycode: %r" % name)

    def type_text(self, text, char_delay_ms=None):
        """Type a literal string. Raises InjectError on an unmappable character."""
        if not text:
            return
        delay_ms = self._default_delay_ms if char_delay_ms is None else char_delay_ms
        try:
            if delay_ms and delay_ms > 0:
                self._layout.write(text, delay=delay_ms / 1000)
            else:
                self._layout.write(text)
        except ValueError as e:
            # Character not in the US layout (e.g. a non-ASCII symbol).
            raise InjectError(str(e))

    def send_chord(self, names):
        """Send a modifier+key chord given a list of keycode names."""
        if not names:
            raise InjectError("empty chord")
        codes = [self._resolve(n) for n in names]
        non_mod = [c for c in codes if not (Keycode.LEFT_CONTROL <= c <= Keycode.RIGHT_GUI)]
        if len(non_mod) > _MAX_KEYS:
            raise InjectError("chord has more than %d non-modifier keys" % _MAX_KEYS)
        self._kbd.send(*codes)

    def sysrq(self, command="b"):
        """Send a Magic SysRq: hold Alt+SysRq, tap the command key, release.

        Unlike send_chord (which presses and releases everything at once), SysRq
        needs Alt+SysRq held down WHILE the command key is tapped, so it is its own
        method. `command` is a single key: 'b'=reboot, 'o'=poweroff, 's'=sync,
        'e'=term, 'i'=kill, 'c'=crash. Requires kernel.sysrq enabled on the target.
        """
        if not isinstance(command, str) or len(command.strip()) != 1:
            raise InjectError("sysrq command must be a single key")
        key = self._resolve(command.strip())
        try:
            self._kbd.press(Keycode.LEFT_ALT)
            self._kbd.press(Keycode.PRINT_SCREEN)   # PrintScreen == SysRq
            time.sleep(0.06)
            self._kbd.press(key)
            time.sleep(0.06)
        finally:
            self._kbd.release_all()

    def run_sequence(self, steps, stop_on_error=False):
        """Run an ordered list of steps, returning (ok, detail).

        Each step is a `type` step, a `keys` step, or a `{delay_ms}` pause. One
        result is returned for the whole sequence. With stop_on_error, the first
        failing step ends the run; otherwise remaining steps still execute and
        the first error is reported.
        """
        if not isinstance(steps, list):
            return False, "steps must be a list"
        first_error = None
        for i, step in enumerate(steps):
            try:
                if not isinstance(step, dict):
                    raise InjectError("step %d is not an object" % i)
                if "delay_ms" in step:
                    ms = step["delay_ms"]
                    if ms and ms > 0:
                        time.sleep(ms / 1000)
                    continue
                stype = step.get("type")
                if stype == "type":
                    self.type_text(step.get("text", ""), step.get("char_delay_ms"))
                elif stype == "keys":
                    self.send_chord(step.get("chord", []))
                else:
                    raise InjectError("step %d has unknown type %r" % (i, stype))
            except InjectError as e:
                if first_error is None:
                    first_error = "step %d: %s" % (i, e)
                if stop_on_error:
                    return False, first_error
        return (first_error is None), first_error
