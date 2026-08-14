// One DBus method for Claude Status Bar: Raise(match) brings the topmost window whose
// class or application id contains `match` to the front. Runs inside gnome-shell, the
// one place allowed to move focus on Wayland.
import Gio from 'gi://Gio';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'io.github.millmaster.ClaudeStatusBarFocus';
const OBJECT_PATH = '/io/github/millmaster/ClaudeStatusBarFocus';
const IFACE = `<node>
  <interface name="${BUS_NAME}">
    <method name="Raise">
      <arg type="s" direction="in" name="match"/>
      <arg type="b" direction="out" name="raised"/>
    </method>
  </interface>
</node>`;

export default class ClaudeStatusBarFocusExtension extends Extension {
    enable() {
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        this._nameId = Gio.DBus.session.own_name(
            BUS_NAME, Gio.BusNameOwnerFlags.NONE, null, null);
    }

    disable() {
        if (this._nameId) {
            Gio.bus_unown_name(this._nameId);
            this._nameId = 0;
        }
        this._dbus?.unexport();
        this._dbus = null;
    }

    Raise(match) {
        const needle = String(match).toLowerCase();
        if (!needle)
            return false;
        const windows = global.get_window_actors().map(a => a.meta_window);
        // Highest in the stack first, so the most recently used match wins.
        const stacked = global.display.sort_windows_by_stacking(windows).reverse();
        for (const win of stacked) {
            const wmClass = (win.get_wm_class() ?? '').toLowerCase();
            const appId = (win.get_gtk_application_id() ?? '').toLowerCase();
            if (wmClass.includes(needle) || appId.includes(needle)) {
                win.activate(global.get_current_time());
                return true;
            }
        }
        return false;
    }
}
