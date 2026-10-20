import contextlib
import io
import json
import os
import runpy
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

import requests

import main
from util import zepp_helper


BEIJING = timezone(timedelta(hours=8))
ACCOUNT = 'daily-test@example.test'


def fake_login(runner):
    runner.user_id = 'test-user-id'
    return 'test-app-token'


class DailyStepTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.dict(os.environ, {'MIN_STEP': '', 'MAX_STEP': ''}))
        self.stack.enter_context(patch.multiple(
            main, create=True, users=ACCOUNT, passwords='test-password',
            user_tokens={ACCOUNT: {'bound_device_id': 'test-device'}},
            min_step=70000, max_step=80000, use_concurrent=False,
            sleep_seconds=0, encrypt_support=False,
            push_config=main.push_util.PushConfig()))
        self.stack.enter_context(patch.object(main.MiMotionRunner, 'login', fake_login))
        self.stack.enter_context(patch.object(main.push_util, 'push_results'))
        self.post = self.stack.enter_context(patch.object(zepp_helper.requests, 'post'))
        self.post.return_value = Mock(status_code=200)
        self.post.return_value.json.return_value = {'message': 'success'}

    def submitted_data(self):
        body = parse_qs(self.post.call_args.kwargs['data'])
        payload = json.loads(body['data_json'][0])[0]
        summary = json.loads(payload['summary'])
        return payload['date'], summary['stp']['ttl']

    def test_variables_override_config_range(self):
        with patch.dict(os.environ, {'MIN_STEP': '70000', 'MAX_STEP': '80000'}):
            self.assertEqual(main.get_step_range({'MIN_STEP': '18000', 'MAX_STEP': '25000'}),
                             (70000, 80000))

    def test_empty_variables_use_config_then_defaults(self):
        self.assertEqual(main.get_step_range({'MIN_STEP': '71000', 'MAX_STEP': '79000'}),
                         (71000, 79000))
        self.assertEqual(main.get_step_range({}), (70000, 80000))

    def test_invalid_range_fails_before_submission(self):
        for minimum, maximum in [('80000', '70000'), ('0', '80000'), ('bad', '80000')]:
            with self.subTest(minimum=minimum, maximum=maximum):
                with self.assertRaises(ValueError):
                    main.get_step_range({'MIN_STEP': minimum, 'MAX_STEP': maximum})
        self.post.assert_not_called()

    def test_repeated_runs_use_full_same_day_target(self):
        submitted = []
        for hour, minute in [(0, 7), (0, 34), (1, 7), (11, 17), (22, 0), (23, 59)]:
            now = datetime(2026, 10, 19, hour, minute, tzinfo=BEIJING)
            with patch.object(main, 'get_beijing_time', return_value=now):
                self.assertTrue(main.execute())
            submitted.append(self.submitted_data())
        self.assertEqual(len(set(submitted)), 1)
        target_date, step = submitted[0]
        self.assertEqual(target_date, '2026-10-19')
        self.assertGreaterEqual(step, 70000)
        self.assertLessEqual(step, 80000)

    def test_delayed_run_uses_actual_beijing_day(self):
        # UTC 19 日 17:07 是北京时间 20 日 01:07，仍应提交完整范围。
        with patch.object(main, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 19, 17, 7, tzinfo=timezone.utc)
            self.assertTrue(main.execute())
        target_date, step = self.submitted_data()
        self.assertEqual(target_date, '2026-10-20')
        self.assertGreaterEqual(step, 70000)
        self.assertLessEqual(step, 80000)

    def test_network_crossing_midnight_keeps_target_and_payload_date_together(self):
        before = datetime(2026, 10, 19, 23, 59, 59, tzinfo=BEIJING)
        after = datetime(2026, 10, 20, 0, 0, 1, tzinfo=BEIJING)
        with patch.object(main, 'get_beijing_time', return_value=before), \
                patch.object(zepp_helper, 'get_beijing_time', return_value=after):
            self.assertTrue(main.execute())
        self.assertEqual(self.submitted_data(),
                         ('2026-10-19', main.get_daily_step(ACCOUNT, '2026-10-19', 70000, 80000)))

    def test_helper_default_date_is_also_beijing_date(self):
        with patch.object(zepp_helper, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 19, 17, 7, tzinfo=timezone.utc)
            self.assertEqual(zepp_helper.post_fake_brand_data('75000', 'test-token', 'test-user'),
                             (True, 'success'))
        self.assertEqual(self.submitted_data(), ('2026-10-20', 75000))

    def test_daily_targets_vary_and_remain_in_range(self):
        days = [(date(2026, 10, 1) + timedelta(days=i)).isoformat() for i in range(60)]
        targets = [main.get_daily_step(ACCOUNT, day, 70000, 80000) for day in days]
        self.assertTrue(all(70000 <= step <= 80000 for step in targets))
        self.assertGreater(len(set(targets)), 1)
        self.assertEqual(main.get_daily_step(ACCOUNT, days[0], 75000, 75000), 75000)

    def test_api_rejection_marks_execution_failed(self):
        self.post.return_value.json.return_value = {'message': 'rejected'}
        self.assertFalse(main.execute())

    def test_network_failure_marks_execution_failed(self):
        self.post.side_effect = requests.Timeout('test timeout')
        self.assertFalse(main.execute())

    def test_cli_failure_has_nonzero_exit_for_workflow_retry(self):
        config = json.dumps({'USER': ACCOUNT, 'PWD': 'test-password'})
        with patch.dict(os.environ, {'CONFIG': config, 'AES_KEY': ''}), \
                patch.object(zepp_helper, 'login_access_token', return_value=(None, 'test denied')):
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(Path(main.__file__)), run_name='__main__')
        self.assertEqual(result.exception.code, 1)


if __name__ == '__main__':
    unittest.main()
