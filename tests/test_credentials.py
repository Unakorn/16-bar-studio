"""Credential lookup and UI handoff tests. All credentials here are fake."""
from contextlib import ExitStack
from pathlib import Path
import sys
import tkinter as tk
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eightbar import credentials
from eightbar.dual_ui import DualStudio
import app as classic


class FakeRegistry:
    HKEY_CURRENT_USER = 'user'
    HKEY_LOCAL_MACHINE = 'machine'
    KEY_READ = 1
    REG_SZ = 1
    REG_EXPAND_SZ = 2

    def __init__(self, user=None, machine=None):
        self.values = {'user': user, 'machine': machine}
        self.reads = []

    def OpenKey(self, hive, path, reserved, access):
        assert reserved == 0 and access == self.KEY_READ
        assert path == ('Environment' if hive == 'user' else
                        r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment')
        self.reads.append(hive)
        if isinstance(self.values[hive], Exception):
            raise self.values[hive]
        handle = ExitStack()
        handle.hive = hive
        return handle

    def QueryValueEx(self, handle, name):
        assert name == 'OPENAI_API_KEY'
        value = self.values[handle.hive]
        if value is None:
            raise FileNotFoundError(name)
        return value if isinstance(value, tuple) else (value, self.REG_SZ)


class CredentialLookupTests(unittest.TestCase):
    def setUp(self):
        self.registry = FakeRegistry(machine='fake-system-key')
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(credentials, 'winreg', self.registry))
        self.stack.enter_context(patch.dict(credentials.os.environ, {}, clear=True))
        self.addCleanup(self.stack.close)

    def test_machine_key_without_inherited_environment(self):
        found = credentials.resolve_api_key()
        self.assertEqual((found.value, found.source), ('fake-system-key', 'machine'))
        self.assertEqual(self.registry.reads, ['user', 'machine'])

    def test_user_saved_key_wins_over_machine(self):
        self.registry.values['user'] = '  fake-user-key  '
        found = credentials.resolve_api_key()
        self.assertEqual((found.value, found.source), ('fake-user-key', 'user'))
        self.assertEqual(self.registry.reads, ['user'])

    def test_process_wins_over_saved_keys(self):
        credentials.os.environ['OPENAI_API_KEY'] = ' fake-process-key '
        found = credentials.resolve_api_key()
        self.assertEqual((found.value, found.source), ('fake-process-key', 'process'))
        self.assertEqual(self.registry.reads, [])

    def test_explicit_override_wins(self):
        credentials.os.environ['OPENAI_API_KEY'] = 'fake-process-key'
        found = credentials.resolve_api_key(' fake-entered-key ')
        self.assertEqual((found.value, found.source), ('fake-entered-key', 'entered'))
        self.assertEqual(self.registry.reads, [])

    def test_empty_values_fall_through(self):
        credentials.os.environ['OPENAI_API_KEY'] = '  '
        self.registry.values['user'] = '\n'
        self.assertEqual(credentials.resolve_api_key('  ').source, 'machine')

    def test_access_denied_falls_through(self):
        self.registry.values['user'] = PermissionError('denied')
        self.assertEqual(credentials.resolve_api_key().source, 'machine')
        self.registry.values['machine'] = PermissionError('denied')
        self.assertFalse(credentials.resolve_api_key().value)

    def test_missing_and_non_string_values_are_ignored(self):
        for invalid in (None, (7, 4), (['fake-key'], 1)):
            self.registry.values['user'] = invalid
            self.registry.values['machine'] = None
            self.assertFalse(credentials.resolve_api_key().value)

    def test_non_windows_and_expand_string_type(self):
        self.registry.values['machine'] = ('fake-expanded-key', self.registry.REG_EXPAND_SZ)
        self.assertEqual(credentials.resolve_api_key().value, 'fake-expanded-key')
        with patch.object(credentials, 'winreg', None):
            self.assertFalse(credentials.resolve_api_key().value)

    def test_saved_values_are_read_fresh(self):
        self.assertEqual(credentials.resolve_api_key().value, 'fake-system-key')
        self.registry.values['machine'] = 'fake-new-system-key'
        self.assertEqual(credentials.resolve_api_key().value, 'fake-new-system-key')
        self.registry.values['machine'] = None
        self.assertFalse(credentials.resolve_api_key().value)

    def test_status_and_repr_never_contain_fake_secret(self):
        self.assertIn('Windows system', credentials.key_status())
        self.assertNotIn('fake-system-key', credentials.key_status())
        self.assertNotIn('fake-system-key', repr(credentials.resolve_api_key()))
        self.assertNotIn('fake-entered-key', credentials.key_status('fake-entered-key'))
        self.registry.values['machine'] = None
        self.assertIn('No PC API key found', credentials.key_status())


@unittest.skipUnless('--ui' in sys.argv, 'Run this module with --ui to exercise Tk widgets.')
class CredentialUIHandoffTests(unittest.TestCase):
    def setUp(self):
        self.registry = FakeRegistry(machine='fake-system-key')
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(credentials, 'winreg', self.registry))
        self.stack.enter_context(patch.dict(credentials.os.environ, {'OPENAI_API_KEY': ''}))
        self.addCleanup(self.stack.close)
        self.root = tk.Tk()
        self.root.withdraw()

    def test_dual_empty_entry_detects_pc_key_and_resolves_at_export(self):
        studio = DualStudio(self.root)
        self.addCleanup(studio.close)
        self.assertEqual(studio.api_key.get(), '')
        self.assertIn('Windows system', studio.api_key_status.get())
        self.assertIn('4.5', self.root.title())
        self.root.attributes('-alpha', 0)
        self.root.geometry('940x780')
        self.root.deiconify()
        self.root.update()
        status_label = next(child for child in studio.ai_options.winfo_children()
                            if isinstance(child, classic.ttk.Label)
                            and str(child.cget('textvariable')) == str(studio.api_key_status))
        for widget in (status_label, studio.export_button, studio.personal_style_box, studio.drum_style_box,
                       studio.drum_map_buttons['A'], studio.drum_map_buttons['B']):
            self.assertGreater(widget.winfo_width(), 1)
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                 self.root.winfo_rooty() + self.root.winfo_height())
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                                 self.root.winfo_rootx() + self.root.winfo_width())
        self.registry.values['machine'] = 'fake-new-system-key'
        with patch.object(studio, '_snapshot', return_value={'direction': 'test'}), \
                patch.object(studio, '_start_export') as start:
            studio.export()
            self.assertEqual(start.call_args.kwargs['settings'].api_key, 'fake-new-system-key')
            self.assertEqual(studio.api_key.get(), '')
            studio.api_key.set('fake-entered-key')
            self.assertIn('entered here', studio.api_key_status.get())
            studio.export()
            self.assertEqual(start.call_args.kwargs['settings'].api_key, 'fake-entered-key')
            studio.api_key.set('')
            self.assertIn('Windows system', studio.api_key_status.get())

    def test_dual_missing_key_stops_without_network(self):
        self.registry.values['machine'] = None
        studio = DualStudio(self.root)
        self.addCleanup(studio.close)
        with patch.object(studio, '_snapshot', return_value={'direction': 'test'}), \
                patch.object(studio, '_start_export') as start, \
                patch('eightbar.dual_ui.messagebox.showerror') as error:
            studio.export()
            start.assert_not_called()
            error.assert_called_once()
        self.assertIn('No PC API key found', studio.api_key_status.get())

    def test_classic_keeps_saved_key_out_of_entry_and_resolves_at_arrange(self):
        studio = classic.Studio(self.root)
        self.addCleanup(lambda: (studio.progress.stop(), setattr(studio, 'busy', False), studio._close()))
        self.assertEqual(studio.api_key, '')
        self.assertIn('4.5', self.root.title())
        self.root.attributes('-alpha', 0)
        self.root.deiconify()
        studio.ai_settings()
        dialogs = [child for child in self.root.winfo_children() if isinstance(child, tk.Toplevel)]
        self.assertEqual(len(dialogs), 1)
        dialogs[0].attributes('-alpha', 0)
        self.root.update()
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        entries = [child for child in descendants(dialogs[0]) if isinstance(child, classic.ttk.Entry)]
        key_entry = next(child for child in entries if child.cget('show'))
        self.assertEqual(key_entry.get(), '')
        save_button = next(child for child in descendants(dialogs[0])
                           if isinstance(child, classic.ttk.Button) and child.cget('text') == 'Save settings')
        self.assertGreater(save_button.winfo_height(), 1)
        self.assertLessEqual(save_button.winfo_rooty() + save_button.winfo_height(),
                             dialogs[0].winfo_rooty() + dialogs[0].winfo_height())
        dialogs[0].destroy()
        self.registry.values['machine'] = 'fake-new-system-key'
        studio.source = object()
        studio.arrangement = object()
        studio.original_arrangement = object()
        with patch.object(studio, 'current', return_value=None), \
                patch.object(studio, '_refresh_plan'), \
                patch.object(studio, '_save_ai_preferences'), \
                patch.object(studio, '_freeze'), \
                patch('app.threading.Thread'), \
                patch('app.AISettings', wraps=classic.AISettings) as settings:
            studio.arrange_ai()
            self.assertEqual(settings.call_args.kwargs['api_key'], 'fake-new-system-key')
            self.assertEqual(studio.api_key, '')


if __name__ == '__main__':
    if '--ui' in sys.argv:
        sys.argv.remove('--ui')
    unittest.main()
