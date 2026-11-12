from contextlib import redirect_stdout
import io
from pathlib import Path
import unittest
from unittest.mock import patch
from tools import claude_manual_login as manual
from tools import claude_account_menu as menu


class RecoveryTests(unittest.TestCase):
    def test_manual_recovery_closes_only_selected_profile_after_confirmation(self):
        with patch.object(manual.sys,'platform','win32'), \
             patch.object(manual,'installed_browsers',return_value={'edge':Path('msedge.exe')}), \
             patch.object(manual,'launch_browser',side_effect=[manual.ProfileInCheckMode(),None]) as launch, \
             patch.object(manual,'close_check_profile') as close, \
             patch('builtins.input',return_value='ДА'), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(manual.main(['--account-id','2','--browser','edge']),0)
            close.assert_called_once_with(manual.profile_path(2,'edge'))
            self.assertEqual(launch.call_count,2)
            self.assertEqual(launch.call_args_list[0],launch.call_args_list[1])
            self.assertIn('Личные профили и база не затрагиваются',output.getvalue())

    def test_declining_recovery_does_not_close_or_relaunch(self):
        with patch.object(manual.sys,'platform','win32'), \
             patch.object(manual,'installed_browsers',return_value={'edge':Path('msedge.exe')}), \
             patch.object(manual,'launch_browser',side_effect=manual.ProfileInCheckMode()) as launch, \
             patch.object(manual,'close_check_profile') as close, \
             patch('builtins.input',return_value=''), redirect_stdout(io.StringIO()):
            self.assertEqual(manual.main(['--account-id','2','--browser','edge']),1)
            close.assert_not_called()
            launch.assert_called_once()

    def test_recovery_failure_does_not_open_another_profile_or_print_secrets(self):
        with patch.object(manual.sys,'platform','win32'), \
             patch.object(manual,'installed_browsers',return_value={'edge':Path('msedge.exe')}), \
             patch.object(manual,'launch_browser',side_effect=manual.ProfileInCheckMode()) as launch, \
             patch.object(manual,'close_check_profile',side_effect=RuntimeError('SECRET')), \
             patch('builtins.input',return_value='ДА'), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(manual.main(['--account-id','2','--browser','edge']),2)
            launch.assert_called_once()
            self.assertNotIn('SECRET',output.getvalue())

    def test_menu_routes_manual_and_check_separately(self):
        with patch('builtins.input',side_effect=['bad','1','2','0']), \
             patch.object(menu.manual,'main',return_value=0) as login, \
             patch.object(menu.check,'main',return_value=0) as check, redirect_stdout(io.StringIO()):
            self.assertEqual(menu.main(),0)
            login.assert_called_once_with([])
            check.assert_called_once_with()


if __name__ == '__main__':unittest.main()
