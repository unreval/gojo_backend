"""Image vision attempts must each receive a timeout slice, not the whole budget."""
import unittest
from unittest.mock import patch

import anthropic
import httpx

from tests.test_model_failures import REPLY_WITH_VISUAL, model_response


def api_timeout():
    return anthropic.APITimeoutError(
        httpx.Request('POST', 'https://api.anthropic.com/v1/messages'))


class ImageTimeoutBudgetTests(unittest.TestCase):
    def setUp(self):
        from tests.test_model_failures import ImageFailureTests
        self.host = ImageFailureTests(
            'test_source_event_id_is_forwarded_to_user_memory')
        self.host.setUp()
        self.addCleanup(self.host.doCleanups)
        self.route = self.host.route
        self.client = self.host.client
        self.memory = self.host.memory
        self.clock = {'now': 1_000_000.0}
        patcher = patch.object(self.route.time, 'monotonic', self._monotonic)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _monotonic(self):
        return self.clock['now']

    def send(self, **extra):
        return self.host.send(**extra)

    def _timeouts(self):
        return [
            call.kwargs['timeout']
            for call in self.client.messages.create.call_args_list
        ]

    def _assert_sdk_retries_stay_off(self):
        self.assertGreaterEqual(self.route.get_anthropic.call_count, 1)
        for call in self.route.get_anthropic.call_args_list:
            self.assertEqual(call.kwargs.get('max_retries'), 0)

    def _consume_and_timeout(self, *_args, **kwargs):
        self.clock['now'] += float(kwargs['timeout'])
        raise api_timeout()

    def test_timeout_then_valid_json_uses_the_second_attempt(self):
        calls = {'n': 0}

        def create(*_args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                self.clock['now'] += float(kwargs['timeout'])
                raise api_timeout()
            return model_response(REPLY_WITH_VISUAL)

        self.client.messages.create.side_effect = create
        response, result = self.send(source_event_id='img-timeout-retry')

        self.assertEqual(calls['n'], 2)
        self.assertEqual(self.client.messages.create.call_count, 2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(result['messages'][0]['jp'], '赤い画像が見えるよ。')
        self.assertNotIn('error', result)
        self.memory.save_user_short_memory_once.assert_called_once()
        self.assertEqual(self.memory.save_short_memory.call_count, 1)
        self._assert_sdk_retries_stay_off()

    def test_first_attempt_timeout_is_the_slice_not_the_total_budget(self):
        self.client.messages.create.return_value = model_response(REPLY_WITH_VISUAL)
        self.send()
        timeout = self._timeouts()[0]
        self.assertEqual(timeout, self.route.IMAGE_ATTEMPT_TIMEOUT_SECONDS)
        self.assertEqual(self.route.IMAGE_ATTEMPT_TIMEOUT_SECONDS, 55)
        self.assertNotEqual(timeout, self.route.IMAGE_TOTAL_BUDGET_SECONDS)
        self.assertEqual(self.route.IMAGE_TOTAL_BUDGET_SECONDS, 120)

    def test_second_attempt_still_receives_a_full_slice(self):
        calls = {'n': 0}

        def create(*_args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                self.clock['now'] += float(kwargs['timeout'])
                raise api_timeout()
            return model_response(REPLY_WITH_VISUAL)

        self.client.messages.create.side_effect = create
        response, _result = self.send()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._timeouts(), [
            self.route.IMAGE_ATTEMPT_TIMEOUT_SECONDS,
            self.route.IMAGE_ATTEMPT_TIMEOUT_SECONDS,
        ])

    def test_first_success_calls_the_model_once_with_the_slice(self):
        self.client.messages.create.return_value = model_response(REPLY_WITH_VISUAL)
        response, result = self.send()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.messages.create.call_count, 1)
        self.assertEqual(
            self._timeouts(), [self.route.IMAGE_ATTEMPT_TIMEOUT_SECONDS])
        self.assertEqual(result['messages'][0]['zh'], '能看到红色的图片。')

    def test_exhausted_budget_stops_after_three_timeouts(self):
        self.client.messages.create.side_effect = self._consume_and_timeout
        response, result = self.send(source_event_id='img-timeout-exhausted')
        timeouts = self._timeouts()
        self.assertEqual(len(timeouts), 3)
        self.assertLessEqual(len(timeouts), 3)
        self.assertEqual(timeouts, [
            self.route.IMAGE_ATTEMPT_TIMEOUT_SECONDS,
            self.route.IMAGE_ATTEMPT_TIMEOUT_SECONDS,
            10,
        ])
        self.assertEqual(response.status_code, 502)
        self.assertEqual(result['error'], 'generation_failed')
        self.assertTrue(result['generation_failed'])
        self.assertEqual(result['messages'], [])
        self.memory.save_short_memory.assert_not_called()

    def test_fast_invalid_json_still_uses_three_attempts(self):
        bad = model_response('{"emotion":"平静","messages":[]}')
        self.client.messages.create.side_effect = [bad, bad, bad]
        response, result = self.send()
        self.assertEqual(self.client.messages.create.call_count, 3)
        self.assertEqual(self._timeouts(), [
            self.route.IMAGE_ATTEMPT_TIMEOUT_SECONDS,
            self.route.IMAGE_ATTEMPT_TIMEOUT_SECONDS,
            self.route.IMAGE_ATTEMPT_TIMEOUT_SECONDS,
        ])
        self.assertEqual(self.clock['now'], 1_000_000.0)
        self.host.assert_generation_failed(response, result)

    def test_status_400_stops_after_one_attempt(self):
        error = RuntimeError('invalid image request')
        error.status_code = 400
        self.client.messages.create.side_effect = error
        response, result = self.send()
        self.assertEqual(self.client.messages.create.call_count, 1)
        self.assertFalse(result['retryable'])
        self.host.assert_generation_failed(response, result)

    def test_free_path_failure_persists_placeholder_visual_summary(self):
        bad = model_response('{"emotion":"平静","messages":[]}')
        self.client.messages.create.side_effect = [bad, bad, bad]
        response, result = self.send(text='看这张图')
        self.host.assert_generation_failed(response, result)
        meta = self.memory.save_user_short_memory_once.call_args.kwargs['event_meta']
        summary = meta['visual_summary']
        display_text = '📷 看这张图'
        self.assertTrue(str(summary).strip())
        self.assertIn(display_text, summary)
        self.assertEqual(
            summary,
            self.route._build_visual_summary(None, display_text, False, 1))
        self.memory.save_user_short_memory_once.assert_called_once()

    def test_free_path_success_uses_real_visual_summary(self):
        self.client.messages.create.return_value = model_response(REPLY_WITH_VISUAL)
        response, result = self.send(text='看这张图')
        static = self.route._build_visual_summary(None, '📷 看这张图', False, 1)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            result['visual_summary'], '画面里是一块红色区域，没有可读文字。')
        self.assertEqual(result['event_meta']['visual_summary'], result['visual_summary'])
        self.assertNotEqual(result['event_meta']['visual_summary'], static)
        self.assertNotIn('用户发来图片', result['event_meta']['visual_summary'])

    def test_busy_path_still_analyzes_once_and_merges_that_summary(self):
        import sys
        from unittest.mock import Mock
        from tests.test_model_failures import stub

        captured = {}
        merge = Mock()

        def _check(*_args, **kwargs):
            captured['event_meta'] = kwargs.get('event_meta')
            return {
                'can_reply': False,
                'seen': False,
                'reply_state': 'hard_busy',
                'opportunity_id': 4,
                'pending_count': 1,
                'activity': {},
            }

        with patch.dict(sys.modules, {
            'reply_availability': stub(
                'reply_availability', check_reply_availability=_check),
            'db_schedule': stub(
                'db_schedule', merge_phone_check_event_meta=merge),
        }), patch.object(
            self.route, '_analyze_visual_summary', return_value='图像摘要',
        ) as analyze:
            response, result = self.send(source_event_id='image-busy')

        self.assertEqual(response.status_code, 200)
        analyze.assert_called_once()
        self.client.messages.create.assert_not_called()
        self.assertNotIn('visual_summary', captured.get('event_meta') or {})
        merge.assert_called_once()
        self.assertEqual(merge.call_args.args[1]['visual_summary'], '图像摘要')
        self.assertEqual(result['visual_summary'], '图像摘要')
        self.assertEqual(result['event_meta']['visual_summary'], '图像摘要')


if __name__ == '__main__':
    unittest.main()
