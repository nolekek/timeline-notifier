"""Offline regression checks for opt-in UI, ownership, delivery and migrations."""
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEMP = tempfile.TemporaryDirectory()
os.environ['DATABASE_URL'] = 'sqlite:///' + str(Path(TEMP.name) / 'planner.db')
os.environ['SECRET_KEY'] = 'planner-offline-test-secret'
os.environ['CRED_KEY'] = 'planner-offline-test-secret'
os.environ['ADMIN_PASSWORD'] = 'offline-test'

from app import create_app, _auto_migrate
from app.models import db, User, WorkItem, PlannerEvent, utc_now
from app.bot import planner
from app.bot.planner_jobs import _process_user, record_change, schedule_delta
from app.bot.telegram_bot import TelegramBot


class Sender:
    def __init__(self, ok=True):
        self.ok = ok
        self.messages = []
        self.answers = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))
        return self.ok

    def edit_message(self, chat_id, message_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))

    def answer_callback(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    _main_keyboard = staticmethod(TelegramBot._main_keyboard)


class PlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            db.engine.dispose()
        TEMP.cleanup()

    def setUp(self):
        self.context = self.app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()
        self.user = User(chat_id='1', is_registered=True, state='ready')
        self.other = User(chat_id='2', is_registered=True, state='ready')
        db.session.add_all([self.user, self.other])
        db.session.flush()
        self.work = WorkItem(user_id=self.user.id, external_id='10', discipline='Информатика',
                             deadline=(planner.now_local() + timedelta(minutes=40)).strftime('%d.%m.%Y %H:%M'),
                             status='Не отправлена', point_name='Лабораторная')
        db.session.add(self.work)
        db.session.commit()
        self.bot = Sender()

    def tearDown(self):
        db.session.remove()
        self.context.pop()

    def enable(self):
        self.user.new_unlocked = self.user.theme_new = True
        self.user.quiet_start = self.user.quiet_end = '00:00'
        db.session.commit()

    def callback(self, user, data):
        return planner.handle_callback(self.bot, user, user.chat_id, 1, 'cb', data)

    def test_unlock_does_not_activate_and_selection_is_personal(self):
        self.assertFalse(planner.active(self.user))
        self.callback(self.user, 'theme|new')
        self.assertFalse(planner.active(self.user))
        planner.handle_message(self.bot, self.user, '1', '/new')
        self.assertTrue(self.user.new_unlocked)
        self.assertFalse(self.user.theme_new)
        self.callback(self.user, 'theme|new')
        self.assertTrue(planner.active(self.user))
        self.assertFalse(planner.active(self.other))
        self.callback(self.user, 'theme|legacy')
        self.assertFalse(planner.active(self.user))
        self.assertFalse(self.user.theme_femboy)

    def test_progress_and_notes_are_owned_and_do_not_change_official_status(self):
        self.enable()
        self.callback(self.user, f'p|setprogress|{self.work.id}|ready')
        self.assertEqual(self.work.personal_progress, 'ready')
        self.assertEqual(self.work.status, 'Не отправлена')
        self.other.new_unlocked = self.other.theme_new = True
        self.callback(self.other, f'p|setprogress|{self.work.id}|todo')
        self.assertEqual(self.work.personal_progress, 'ready')
        self.callback(self.user, f'p|note|{self.work.id}')
        planner.handle_message(self.bot, self.user, '1', '<b>моя заметка</b>')
        text, _ = planner.card_view(self.user, self.work)
        self.assertIn('&lt;b&gt;моя заметка&lt;/b&gt;', text)
        self.assertIsNone(self.user.planner_input)

    def test_delivery_failure_does_not_consume_reminder_and_success_deduplicates(self):
        self.enable()
        sender = Sender(False)
        _process_user(sender, self.user)
        self.assertFalse(self.work.planner_reminded)
        sender.ok = True
        _process_user(sender, self.user)
        self.assertIn('1', self.work.planner_reminded.split(','))
        count = len(sender.messages)
        _process_user(sender, self.user)
        self.assertEqual(len(sender.messages), count)

    def test_quiet_hours_and_muted_subject_do_not_consume_reminders(self):
        self.enable()
        self.user.quiet_start, self.user.quiet_end = '00:00', '23:59'
        self.user.urgent_at_night = False
        with patch('app.bot.planner_jobs.now_local', return_value=planner.now_local().replace(hour=12)):
            _process_user(self.bot, self.user)
        self.assertFalse(self.bot.messages)
        self.assertFalse(self.work.planner_reminded)
        self.user.quiet_end = '00:00'
        self.user.muted_disciplines = '["Информатика"]'
        _process_user(self.bot, self.user)
        self.assertFalse(self.bot.messages)

    def test_snooze_survives_session_and_is_delivered_once(self):
        self.enable()
        self.callback(self.user, f'p|setsnooze|{self.work.id}|30')
        self.bot.messages.clear()
        _process_user(self.bot, self.user)
        self.assertFalse(self.bot.messages)
        self.work.snooze_until = planner.now_local() - timedelta(seconds=1)
        db.session.commit()
        db.session.expire_all()
        _process_user(self.bot, self.user)
        self.assertIsNone(self.work.snooze_until)
        self.assertEqual(len(self.bot.messages), 1)
        _process_user(self.bot, self.user)
        self.assertEqual(len(self.bot.messages), 1)

    def test_change_delivery_retries_and_history_remains(self):
        self.enable()
        record_change(self.user, self.work, [('Баллы', '0', '5')])
        db.session.commit()
        sender = Sender(False)
        self.work.planner_reminded = '1,12,24'
        _process_user(sender, self.user)
        event = PlannerEvent.query.one()
        self.assertIsNone(event.sent_at)
        self.assertEqual(event.attempts, 1)
        event.due_at = utc_now() - timedelta(seconds=1)
        sender.ok = True
        _process_user(sender, self.user)
        self.assertIsNotNone(event.sent_at)
        self.assertIn('0 → <b>5</b>', event.text)

    def test_week_survives_unavailable_schedule_and_cards_fit_telegram(self):
        self.enable()
        with patch('app.parser.schedule.get_group_schedule', side_effect=RuntimeError('offline')):
            text, _ = planner.week_view(self.user)
        self.assertIn('Расписание не получено', text)
        self.work.control_text = '<&' * 5000
        self.work.personal_note = '&' * 500
        text, kb = planner.card_view(self.user, self.work)
        self.assertLess(len(text), 4096)
        for row in kb['inline_keyboard']:
            for b in row:
                self.assertLessEqual(len(b.get('callback_data', '').encode()), 64)

    def test_migration_adds_defaults_without_activating_theme(self):
        db.session.remove()
        db.drop_all()
        db.session.execute(db.text('CREATE TABLE bot_user (id INTEGER PRIMARY KEY, chat_id VARCHAR(32))'))
        db.session.execute(db.text("INSERT INTO bot_user VALUES (7, 'old-user')"))
        db.session.execute(db.text('CREATE TABLE user_work_item (id INTEGER PRIMARY KEY)'))
        db.session.execute(db.text('CREATE TABLE setting (id INTEGER PRIMARY KEY)'))
        db.session.commit()
        _auto_migrate()
        _auto_migrate()
        row = db.session.execute(db.text('SELECT theme_new, new_unlocked, quiet_start FROM bot_user WHERE id=7')).one()
        self.assertEqual(tuple(row), (0, 0, '23:00'))

    def test_schedule_delta_reports_specific_fields(self):
        a = [{'day': '23 ноября', 'lessons': [{'subject': 'Психология', 'time': '10:40', 'classroom': '305'}]}]
        b = [{'day': '23 ноября', 'lessons': [{'subject': 'Психология', 'time': '12:30', 'classroom': '412'}]}]
        text = schedule_delta(a, b)
        self.assertIn('10:40 → <b>12:30</b>', text)
        self.assertIn('305 → <b>412</b>', text)

    def test_scheduler_syncs_every_six_hours_in_both_modes(self):
        from app.scheduler import jobs
        for low_power in (True, False):
            with patch.object(jobs, '_scheduler', None), patch.object(jobs, 'BackgroundScheduler') as factory:
                with patch.dict(self.app.config, {'LOW_POWER_MODE': low_power}):
                    jobs.start_scheduler(self.app)
                factory.assert_called_once_with(daemon=True, timezone='Europe/Moscow')
                calls = factory.return_value.add_job.call_args_list
                sync = next(c for c in calls if c.kwargs.get('id') == 'check_all_users')
                self.assertEqual(sync.kwargs['trigger'], 'cron')
                self.assertEqual(sync.kwargs['hour'], '0,6,12,18')
                reminders = next(c for c in calls if c.kwargs.get('id') == 'planner_notifications')
                self.assertEqual(reminders.kwargs['seconds'], 60)

    def test_partial_timeline_response_is_rejected(self):
        from types import SimpleNamespace
        from app.parser.scraper import TimelineScraper
        from unittest.mock import Mock
        session = Mock()
        response = Mock()
        response.json.return_value = {'Rows': [{'Id': '1'}], 'Total': 2}
        session.get.side_effect = [response, RuntimeError('page two unavailable')]
        auth = SimpleNamespace(get_session=lambda: session, verify_ssl=False)
        scraper = TimelineScraper(auth, 'http://offline/send', 'http://offline/grid', semester_id=30)
        with self.assertRaisesRegex(RuntimeError, 'полную ведомость'):
            scraper.fetch_works()


if __name__ == '__main__':
    unittest.main()
