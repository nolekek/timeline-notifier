import logging
import re
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from app.parser.auth import TimelineAuth

logger = logging.getLogger(__name__)

SKIP_STATUSES = {'Отправлена', 'Проверена', 'Зачтена'}


def _parse_deadline_raw(raw: str) -> datetime | None:
    if not raw:
        return None
    m = re.match(r'/Date\((\d+)\)/', raw)
    if not m:
        return None
    try:
        return datetime.fromtimestamp(int(m.group(1)) / 1000)
    except (OSError, ValueError):
        return None


def _parse_deadline(raw: str) -> str:
    dt = _parse_deadline_raw(raw)
    return dt.strftime('%d.%m.%Y %H:%M') if dt else (raw or '')


def _format_status(row: dict) -> str:
    if row.get('PointAccepted') is True:
        return 'Зачтена'
    if row.get('WorkChecked') is True:
        return 'Проверена'
    ws = row.get('WorkStatusId')
    if ws is not None and ws > 0:
        return 'Отправлена'
    return 'Не отправлена'


class TimelineScraper:
    def __init__(self, auth: TimelineAuth, send_url: str, grid_url: str,
                 semester_id: int = None, debug_dir: str = None):
        self.auth = auth
        self.send_url = send_url
        self.grid_url = grid_url
        self.semester_id = semester_id
        self.debug_dir = debug_dir

    def _dump(self, text: str) -> str | None:
        """Сырой ответ SendGrid на диск — чтобы чинить парсер после правок сайта."""
        if not self.debug_dir:
            return None
        try:
            Path(self.debug_dir).mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            path = Path(self.debug_dir) / f'sendgrid-{stamp}.json'
            path.write_text(text, encoding='utf-8', errors='replace')
            return str(path)
        except OSError as e:
            logger.warning(f'Не удалось сохранить дамп SendGrid: {e}')
            return None

    def _detect_semester_id(self, session) -> int | None:
        resp = session.get(self.send_url, timeout=20,
                           verify=self.auth.verify_ssl)
        resp.raise_for_status()

        # 1. Парсинг через BeautifulSoup
        soup = BeautifulSoup(resp.text, 'html.parser')
        sel = soup.find('select', {'name': re.compile(r'SemesterId', re.I)})
        if sel:
            opt = sel.find('option', selected=True)
            if opt and opt.get('value', '').isdigit():
                return int(opt['value'])
            first = sel.find('option')
            if first and first.get('value', '').isdigit():
                return int(first['value'])

        # 2. Фолбэк на регулярное выражение
        m = re.search(
            r'<select[^>]*name="SemesterId"[^>]*>.*?'
            r'<option[^>]*selected[^>]*value="(\d+)"',
            resp.text, re.DOTALL | re.IGNORECASE,
        )
        return int(m.group(1)) if m else None

    def fetch_works(self) -> list[dict]:
        session = self.auth.get_session()

        if self.semester_id is None:
            self.semester_id = self._detect_semester_id(session)
        if self.semester_id is None:
            logger.error("Не удалось определить semesterId")
            return []
        logger.info(f"Используется semesterId={self.semester_id}")

        headers = {
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': self.send_url,
            'Accept': 'application/json, text/javascript, */*; q=0.01',
        }

        all_rows = []
        page = 1
        max_pages = 5  # Защита от бесконечного цикла, обычно 1-2 страницы

        while page <= max_pages:
            params = {
                'semesterId': self.semester_id,
                'EduModuleId': '',
                'StudWorksReceptionId': '',
                'hideAcceptedCheckbox': 'false',
                '_search': 'false',
                'nd': str(page),
                'rows': '100',
                'page': str(page),
                'sidx': 'Deadline',
                'sord': 'asc',
            }
            try:
                resp = session.get(self.grid_url, params=params, headers=headers,
                                   timeout=20, verify=self.auth.verify_ssl)
                resp.raise_for_status()
            except Exception as e:
                logger.error(f"Ошибка запроса SendGrid (стр. {page}): {e}")
                break

            if page == 1:
                dump_path = self._dump(resp.text)
                if dump_path:
                    logger.info(f"Сырой ответ SendGrid сохранён: {dump_path}")

            try:
                data = resp.json()
            except ValueError:
                logger.error(f"SendGrid вернул не JSON. Ответ: {resp.text[:300]!r}")
                break

            rows = data.get('Rows', [])
            all_rows.extend(rows)

            total_pages = data.get('Total', 1)
            try:
                total_pages = int(total_pages)
            except (ValueError, TypeError):
                total_pages = 1

            if page >= total_pages or not rows:
                break
            page += 1

        logger.info(f"Всего записей в ответе: {len(all_rows)}")

        now = datetime.now()
        works = []
        for row in all_rows:
            work_id = str(row.get('Id', '')).strip()
            if not work_id or not work_id.isdigit():
                continue

            discipline = (row.get('EduModuleName') or '').strip()
            if not discipline:
                continue

            deadline_dt = _parse_deadline_raw(row.get('Deadline', ''))
            deadline_str = deadline_dt.strftime('%d.%m.%Y %H:%M') if deadline_dt else ''
            status = _format_status(row)

            is_overdue = (
                deadline_dt is not None
                and deadline_dt < now
                and status == 'Не отправлена'
            )

            works.append({
                'external_id': work_id,
                'discipline': discipline,
                'deadline': deadline_str,
                'status': status,
                'is_overdue': is_overdue,
            })

        logger.info(f"Обработано работ: {len(works)}")
        return works
