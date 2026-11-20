"""מי מורשה להשתמש בבוט. נשמר בקובץ access.json ליד הבוט (לא עולה ל-GitHub)."""

import json
import secrets
import threading
from pathlib import Path

ACCESS_FILE = Path(__file__).parent / "access.json"


class Access:
    def __init__(self, path: Path = ACCESS_FILE):
        self.path = path
        self._lock = threading.Lock()
        self.owner: int | None = None
        self.allowed: dict[int, str] = {}   # id -> שם
        self.blocked: dict[int, str] = {}
        self.pending: dict[int, str] = {}   # מחכים לאישור (כדי לא לשלוח לבעלים בקשה כפולה)
        self.claim_code: str | None = None
        self._load()

    # --- שמירה וטעינה ---
    def _load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.owner = data.get("owner")
        self.allowed = {int(k): v for k, v in data.get("allowed", {}).items()}
        self.blocked = {int(k): v for k, v in data.get("blocked", {}).items()}

    def _save(self) -> None:
        data = {
            "owner": self.owner,
            "allowed": {str(k): v for k, v in self.allowed.items()},
            "blocked": {str(k): v for k, v in self.blocked.items()},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # --- בעלים ---
    def new_claim_code(self) -> str:
        """קוד חד-פעמי להגדרת הבעלים (מודפס ב-Termux בלבד)."""
        self.claim_code = f"{secrets.randbelow(900000) + 100000}"
        return self.claim_code

    def claim(self, user_id: int, name: str, code: str) -> bool:
        with self._lock:
            if self.owner is not None or not self.claim_code or not secrets.compare_digest(code, self.claim_code):
                return False
            self.owner, self.claim_code = user_id, None
            self.allowed[user_id] = name
            self.blocked.pop(user_id, None)
            self._save()
            return True

    def is_owner(self, user_id: int) -> bool:
        return self.owner is not None and user_id == self.owner

    # --- משתמשים ---
    def is_allowed(self, user_id: int) -> bool:
        return user_id == self.owner or user_id in self.allowed

    def is_blocked(self, user_id: int) -> bool:
        return user_id in self.blocked

    def mark_pending(self, user_id: int, name: str) -> bool:
        """מחזיר True אם זו בקשה חדשה (צריך להודיע לבעלים)."""
        with self._lock:
            if user_id in self.pending:
                return False
            self.pending[user_id] = name
            return True

    def approve(self, user_id: int, name: str | None = None) -> None:
        with self._lock:
            name = name or self.pending.get(user_id) or self.blocked.get(user_id) or str(user_id)
            self.allowed[user_id] = name
            self.pending.pop(user_id, None)
            self.blocked.pop(user_id, None)
            self._save()

    def block(self, user_id: int, name: str | None = None) -> None:
        with self._lock:
            if user_id == self.owner:
                return  # את הבעלים אי אפשר לחסום
            name = name or self.pending.get(user_id) or self.allowed.get(user_id) or str(user_id)
            self.blocked[user_id] = name
            self.pending.pop(user_id, None)
            self.allowed.pop(user_id, None)
            self._save()

    def remove(self, user_id: int) -> None:
        """מסיר הרשאה בלי לחסום (אם יכתוב שוב - תגיע בקשה חדשה)."""
        with self._lock:
            if user_id == self.owner:
                return
            self.allowed.pop(user_id, None)
            self.blocked.pop(user_id, None)
            self.pending.pop(user_id, None)
            self._save()
