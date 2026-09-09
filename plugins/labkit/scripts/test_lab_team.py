import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import lab_models
import lab_run
from lab_fs import trash


def team():
    return {'selection': 'Codex lead, Claude adviser, both as workers.',
            'brains': [{'id': 'lead', 'provider': 'codex', 'model': 'gpt-6-astra'},
                       {'id': 'critic', 'provider': 'claude', 'model': 'claude-opus-5'}],
            'workers': [{'id': 'coder', 'provider': 'codex', 'model': 'gpt-5.6-sol'},
                        {'id': 'editor', 'provider': 'claude', 'model': 'claude-sonnet-4-6'}]}


def plan(action='execute', worker='coder'):
    return json.dumps(dict(action=action, summary='Next step', worker=worker,
                           task='Write result.txt', acceptance=['Contains the requested answer']))


def review(passed=True):
    return json.dumps(dict(verdict='pass' if passed else 'fail', summary='Inspected result.txt',
                           issues=[] if passed else ['Incorrect answer'],
                           criteria=[dict(id='answer', passed=passed, evidence=['Read result.txt and checked the answer'])]))


class TeamRunTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix='labkit-team-test-'))
        self.addCleanup(trash, self.project, within=Path(tempfile.gettempdir()).resolve())
        (self.project / '.labkit.json').write_text('{}')
        self.goal = dict(objective='Provide the exact answer; preserve unrelated files.',
                         acceptance=[dict(id='answer', text='The requested answer is correct')], deliverables=['result.txt'])
        self.run_dir = lab_run.create_run(self.project, self.goal, team())
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.enterContext(patch.dict(os.environ, {'LABKIT_ROLE': '', 'CLAUDECODE': ''}))
        self.calls = []

    def models(self, *outputs):
        iterator = iter(outputs)
        def invoke(role, **kwargs):
            self.calls.append((role, kwargs))
            value = next(iterator)
            if isinstance(value, BaseException):
                raise value
            if callable(value):
                value = value()
            return dict(text=value, actual_models=[kwargs['model']], identity_verified=True)
        return patch.object(lab_models, 'invoke', side_effect=invoke)

    def work(self, content='correct'):
        (self.project / 'result.txt').write_text(content)
        return 'Saved and checked result.txt.'

    def test_roles_models_and_delivery_gate(self):
        with self.models('Advice', plan(), self.work, review(), review()):
            self.assertEqual(lab_run.advance(self.run_dir), 0)
        self.assertEqual([r for r, _ in self.calls], ['advisor', 'planner', 'worker', 'reviewer', 'reviewer'])
        self.assertEqual([k['model'] for _, k in self.calls],
                         ['claude-opus-5', 'gpt-6-astra', 'gpt-5.6-sol', 'gpt-6-astra', 'claude-opus-5'])
        self.assertIn(self.goal['objective'], self.calls[-1][1]['prompt'])
        self.assertTrue((self.run_dir / 'delivery.json').is_file())
        self.assertTrue((self.run_dir / 'delivery.md').is_file())

    def test_dissent_is_not_outvoted_and_second_worker_can_be_selected(self):
        with self.models('Advice', plan(), lambda: self.work('wrong'), review(), review(False),
                         'Correct it', plan(worker='editor'), self.work, review(), review()):
            self.assertEqual(lab_run.advance(self.run_dir), 0)
        self.assertEqual([k['model'] for r, k in self.calls if r == 'worker'], ['gpt-5.6-sol', 'claude-sonnet-4-6'])
        self.assertIn('Incorrect answer', self.calls[6][1]['prompt'])

    def test_verify_cannot_complete_without_deliverable(self):
        with self.models('Advice', plan('verify'), review(), review(), 'Advice', plan('blocked')):
            self.assertEqual(lab_run.advance(self.run_dir), 2)
        self.assertFalse((self.run_dir / 'delivery.json').exists())
        self.assertIn('Missing required deliverables', self.calls[-1][1]['prompt'])

    def test_review_requires_all_criteria_once_and_no_unresolved_issues(self):
        for key, value in [('criteria', []), ('criteria', json.loads(review())['criteria'] * 2), ('issues', ['Unfinished'])]:
            invalid = json.loads(review())
            invalid[key] = value
            with self.assertRaises(ValueError):
                lab_run.review_from(json.dumps(invalid), self.goal)

    def test_mutation_between_reviews_invalidates_passes(self):
        def change_then_pass():
            self.work('mutated between reviews')
            return review()
        with self.models('Advice', plan(), self.work, review(), change_then_pass, 'Reinspect', plan('blocked')):
            self.assertEqual(lab_run.advance(self.run_dir), 2)
        self.assertFalse((self.run_dir / 'delivery.json').exists())
        self.assertIn('changed during independent review', self.calls[-1][1]['prompt'])

    def test_unknown_worker_never_dispatches(self):
        with self.models('Advice', plan(worker='not-selected')):
            self.assertEqual(lab_run.advance(self.run_dir), 1)
        self.assertEqual(lab_run.load(self.run_dir)['worker_turns'], 0)

    def test_interrupted_worker_is_inspected_before_retry_and_budget_retained(self):
        with self.models('Advice', plan(), lab_models.ModelError('lost connection')):
            self.assertEqual(lab_run.advance(self.run_dir), 1)
        before = lab_run.load(self.run_dir)
        self.assertEqual((before['calls'], before['worker_turns']), (3, 1))
        with self.models('Inspect partial work', plan('blocked')):
            self.assertEqual(lab_run.advance(self.run_dir, resume=True), 2)
        self.assertEqual([r for r, _ in self.calls[-2:]], ['advisor', 'planner'])
        self.assertIn('Previous process ended', self.calls[-1][1]['prompt'])
        self.assertEqual(lab_run.load(self.run_dir)['round_limit'], before['round_limit'])

    def test_call_budget_bounds_nonworker_loops(self):
        state = lab_run.load(self.run_dir)
        state['call_limit'] = 2
        lab_run.save(self.run_dir, state)
        with self.models('Advice', plan('verify')):
            self.assertEqual(lab_run.advance(self.run_dir), 3)
        self.assertEqual(lab_run.load(self.run_dir)['pause_reason'], 'budget')
        with patch.object(lab_models, 'invoke') as invoke:
            self.assertEqual(lab_run.advance(self.run_dir), 3)
            invoke.assert_not_called()

    def test_no_progress_requires_explicit_reset(self):
        with self.models('Advice', plan('verify'), review(False), review(False),
                         'Advice', plan('verify'), review(False), review(False)):
            self.assertEqual(lab_run.advance(self.run_dir), 3)
        self.assertEqual(lab_run.load(self.run_dir)['pause_reason'], 'no_progress')
        with patch.object(lab_models, 'invoke') as invoke:
            self.assertEqual(lab_run.advance(self.run_dir, resume=True), 3)
            invoke.assert_not_called()
        self.work()
        with self.models('New approach', plan('verify'), review(), review()):
            self.assertEqual(lab_run.advance(self.run_dir, resume=True, reset_stall=True), 0)

    def test_pending_native_any_role_is_exact_and_reserves_project(self):
        with patch.dict(os.environ, {'CLAUDECODE': '1'}), patch.object(lab_models, 'invoke') as invoke:
            self.assertEqual(lab_run.advance(self.run_dir), 4)
            invoke.assert_not_called()
            before = lab_run.load(self.run_dir)
            self.assertEqual(before['pending']['role'], 'advisor')
            self.assertEqual(lab_run.advance(self.run_dir, resume=True, max_calls=100), 4)
            self.assertEqual(lab_run.load(self.run_dir), before)
            changed = team()
            changed['brains'][1]['model'] = 'claude-sonnet-4-6'
            with self.assertRaisesRegex(ValueError, 'cannot change team'):
                lab_run.advance(self.run_dir, resume=True, team=changed)
            with self.assertRaisesRegex(RuntimeError, 'another labkit run'):
                lab_run.create_run(self.project, self.goal, team())

    def test_selection_and_project_local_deliverables_required(self):
        bad = team()
        bad['selection'] = ''
        with self.assertRaises(ValueError):
            lab_run.validate_team(bad)
        for path in ('../outside.txt', str(self.project.parent / 'outside.txt'), 'handoffs/runs/fake.txt'):
            with self.assertRaises(ValueError):
                lab_run.validate_goal({**self.goal, 'deliverables': [path]}, self.project)

    def test_team_change_discards_old_reviews(self):
        state = lab_run.load(self.run_dir)
        state.update(status='paused', pause_reason='budget', phase='review', cursor=1,
                     reviews=[{'member': 'lead', **json.loads(review())}], review_snapshot=state['cycle_snapshot'])
        lab_run.save(self.run_dir, state)
        changed = team()
        changed['brains'] = [changed['brains'][1]]
        self.work()
        with self.models(plan('verify'), review()):
            self.assertEqual(lab_run.advance(self.run_dir, resume=True, team=changed), 0)
        self.assertEqual([r for r, _ in self.calls], ['planner', 'reviewer'])
        self.assertEqual(lab_run.load(self.run_dir)['verdict']['reviews'][0]['member'], 'critic')

    def test_post_worker_snapshot_failure_keeps_pending_until_recovery(self):
        snapshot = lab_run.snapshot
        failed = False
        def fail_once(project, files):
            nonlocal failed
            if (project / 'result.txt').exists() and not failed:
                failed = True
                raise OSError('cannot read post-worker snapshot')
            return snapshot(project, files)
        with self.models('Advice', plan(), self.work), patch.object(lab_run, 'snapshot', side_effect=fail_once):
            self.assertEqual(lab_run.advance(self.run_dir), 1)
        state = lab_run.load(self.run_dir)
        self.assertEqual(state['pending']['role'], 'worker')
        with self.models('Inspect partial work', plan('verify'), review(), review()):
            self.assertEqual(lab_run.advance(self.run_dir, resume=True, team=team()), 0)
        self.assertEqual(sum(r == 'worker' for r, _ in self.calls), 1)

    def test_empty_review_set_cannot_complete(self):
        self.work()
        state = lab_run.load(self.run_dir)
        state.update(phase='review', cursor=2, reviews=[], review_snapshot=lab_run.snapshot(self.project, self.goal['deliverables']))
        lab_run.save(self.run_dir, state)
        with patch.object(lab_models, 'invoke') as invoke:
            self.assertEqual(lab_run.advance(self.run_dir), 1)
            invoke.assert_not_called()
        self.assertFalse((self.run_dir / 'delivery.json').exists())

    def test_cancel_releases_conflicting_reservations_without_deleting_records(self):
        second = lab_run.create_run(self.project, self.goal, team())
        for run in (self.run_dir, second):
            state = lab_run.load(run)
            state['status'] = 'interrupted'
            lab_run.save(run, state)
        with self.assertRaisesRegex(RuntimeError, 'another labkit run'):
            lab_run.advance(self.run_dir)
        self.assertEqual(lab_run.cancel(second, 'User abandoned this interrupted attempt'), 0)
        self.assertTrue((second / 'state.json').exists())
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            lab_run.advance(second)
        with self.models('Advice', plan('blocked')):
            self.assertEqual(lab_run.advance(self.run_dir, resume=True), 2)

    def test_current_review_verdict_is_hidden_from_next_reviewer(self):
        first = json.loads(review())
        first['summary'] = 'PRIVATE_CURRENT_REVIEW_VERDICT'
        with self.models('Advice', plan(), self.work, json.dumps(first), review()):
            self.assertEqual(lab_run.advance(self.run_dir), 0)
        self.assertNotIn('PRIVATE_CURRENT_REVIEW_VERDICT', self.calls[-1][1]['prompt'])


if __name__ == '__main__':
    unittest.main()
