#!/usr/bin/env python3
"""
Monitor wolnych terminów – WOMP Katowice, Poradnia dla Kierowców.

Strona https://womp.info/...terminarz?use_service_id=14742 osadza widget
rezerwacji Bookero, który ładuje terminy JavaScriptem. Zamiast renderować
stronę przeglądarką (Playwright/Selenium), odpytujemy bezpośrednio publiczne
API JSON, z którego korzysta sam widget – jest to szybsze, lżejsze i dużo
odporniejsze na zmiany wyglądu strony.

Logika:
  1. Pobierz najbliższy wolny termin (pole `first_free_term`).
     Gdy go brak – przeszukaj kolejne miesiące dzień po dniu (fallback).
  2. Wczytaj ostatnio widziany termin z pliku stanu (last_date.txt).
  3. Jeśli nowy termin jest WCZEŚNIEJSZY niż zapisany -> wyślij e-mail.
  4. Zapisz aktualny termin do pliku stanu (commit robi workflow GitHub Actions).

Kody wyjścia:
  0 – sukces (niezależnie od tego, czy wysłano maila)
  1 – błąd (strona/API niedostępne, zmiana struktury danych, błąd SMTP).
      Workflow kończy się wtedy na czerwono, a GitHub wysyła powiadomienie
      o nieudanym przebiegu. Stan NIE jest wtedy nadpisywany.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- #
# Konfiguracja
# --------------------------------------------------------------------------- #

# Identyfikatory odczytane z kodu widgetu na stronie WOMP.
BOOKERO_ID = os.getenv("BOOKERO_ID", "SW9baU1AxNNH")
SERVICE_ID = os.getenv("SERVICE_ID", "14742")  # usługa "Kierowca"

API_URL = "https://plugin.bookero.pl/plugin-api/v2/getMonth"
BOOKING_PAGE = (
    "https://womp.info/index.php/poradnia/poradnia-dla-kierowcow/"
    "2-uncategorised/36-terminarz?use_service_id=" + SERVICE_ID
)

STATE_FILE = Path(os.getenv("STATE_FILE", "last_date.txt"))
MONTHS_AHEAD = int(os.getenv("MONTHS_AHEAD", "12"))  # zasięg fallbacku
DATE_FMT = "%Y-%m-%d %H:%M"  # format zwracany przez API, np. "2027-01-14 07:00"

# Czy wysłać maila przy pierwszym uruchomieniu (brak pliku stanu)?
NOTIFY_ON_FIRST_RUN = os.getenv("NOTIFY_ON_FIRST_RUN", "false").lower() == "true"

# Tryb testowy: wysyła maila z aktualnym terminem i NIE zmienia pliku stanu.
TEST_EMAIL = os.getenv("TEST_EMAIL", "false").lower() == "true"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("monitor")


class StructureChangedError(RuntimeError):
    """Odpowiedź API nie ma oczekiwanej struktury (np. Bookero zmieniło API)."""


# --------------------------------------------------------------------------- #
# Pobieranie danych
# --------------------------------------------------------------------------- #

def make_session() -> requests.Session:
    """Sesja HTTP z automatycznym ponawianiem przy błędach 429/5xx i timeoutach."""
    retry = Retry(
        total=4,
        backoff_factor=2,  # 0s, 2s, 4s, 8s...
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) termin-monitor/1.0",
            "Accept": "application/json",
            "Referer": BOOKING_PAGE,
        }
    )
    return session


def fetch_month(session: requests.Session, plus_months: int) -> dict:
    """Pobiera kalendarz miesiąca (0 = bieżący, 1 = następny, ...)."""
    params = {
        "bookero_id": BOOKERO_ID,
        "lang": "pl",
        "periodicity_id": 0,
        "custom_duration_id": 0,
        "service": SERVICE_ID,
        "worker": 0,
        "plugin_comment": '{"data":{"parameters":{}}}',
        "phone": "",
        "people": 1,
        "email": "",
        "plus_months": plus_months,
    }
    resp = session.get(API_URL, params=params, timeout=30)
    if resp.status_code != 200:
        # Po wyczerpaniu ponowień (np. trwały błąd 500) – zgłaszamy błąd.
        raise requests.HTTPError(
            f"API zwróciło HTTP {resp.status_code} (plus_months={plus_months})",
            response=resp,
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise StructureChangedError(
            f"Odpowiedź nie jest JSON-em: {resp.text[:200]!r}"
        ) from exc

    # Walidacja kluczowych pól – wykryje zmianę struktury API.
    if not isinstance(data, dict) or data.get("result") != 1 or "days" not in data:
        raise StructureChangedError(
            f"Nieoczekiwana struktura odpowiedzi: {str(data)[:300]}"
        )
    return data


def parse_term(value: str) -> datetime:
    """Zamienia tekst "YYYY-MM-DD HH:MM" na datetime (czas lokalny PL)."""
    try:
        return datetime.strptime(value.strip(), DATE_FMT)
    except (ValueError, AttributeError) as exc:
        raise StructureChangedError(f"Nieznany format daty: {value!r}") from exc


def earliest_in_month(month: dict) -> datetime | None:
    """Fallback: szuka pierwszego dnia z wolną godziną w danych miesiąca."""
    days = month.get("days") or {}
    if isinstance(days, list):  # API zwykle zwraca słownik, ale na wszelki wypadek
        days = {str(i): d for i, d in enumerate(days)}
    candidates: list[datetime] = []
    for day in days.values():
        if not isinstance(day, dict) or day.get("passed") or not day.get("valid_day"):
            continue
        if day.get("first_free_term"):
            candidates.append(parse_term(day["first_free_term"]))
            continue
        for hour in day.get("hours") or []:
            if hour.get("valid") and hour.get("free_slots", 0) > 0:
                candidates.append(parse_term(f"{day['date']} {hour['hour']}"))
    return min(candidates) if candidates else None


def get_nearest_term(session: requests.Session) -> datetime | None:
    """Zwraca najbliższy wolny termin albo None, jeśli nie ma żadnego."""
    current = fetch_month(session, 0)

    # 1) Najprostsza ścieżka: API samo podaje najbliższy wolny termin globalnie.
    ftt = current.get("first_free_term")
    if ftt:
        log.info("API: first_free_term = %s", ftt)
        return parse_term(ftt)

    # 2) Fallback: przeglądamy kolejne miesiące.
    log.warning("Brak pola first_free_term – przeszukuję %d mies.", MONTHS_AHEAD)
    for plus in range(0, MONTHS_AHEAD + 1):
        month = current if plus == 0 else fetch_month(session, plus)
        found = earliest_in_month(month)
        if found:
            return found
    return None


# --------------------------------------------------------------------------- #
# Stan
# --------------------------------------------------------------------------- #

def load_state() -> datetime | None:
    """Wczytuje ostatnio widziany termin. Brak/uszkodzony plik -> None."""
    if not STATE_FILE.exists():
        return None
    raw = STATE_FILE.read_text(encoding="utf-8").strip()
    if not raw or raw == "NONE":
        return None
    try:
        return datetime.strptime(raw, DATE_FMT)
    except ValueError:
        log.warning("Uszkodzony plik stanu (%r) – traktuję jak pusty.", raw)
        return None


def save_state(term: datetime | None) -> None:
    STATE_FILE.write_text(
        (term.strftime(DATE_FMT) if term else "NONE") + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# E-mail
# --------------------------------------------------------------------------- #

def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Brak zmiennej środowiskowej {name} (GitHub Secret).")
    return value


def send_email(new: datetime | None, old: datetime | None, test: bool = False) -> None:
    server = require_env("SMTP_SERVER")
    port = int(os.getenv("SMTP_PORT", "465"))
    user = require_env("SMTP_USER")
    password = require_env("SMTP_PASS")
    mail_to = require_env("MAIL_TO")
    mail_from = os.getenv("MAIL_FROM") or user

    old_txt = old.strftime(DATE_FMT) if old else "brak"
    new_txt = new.strftime(DATE_FMT) if new else "brak wolnych terminów"
    msg = EmailMessage()
    msg["From"] = mail_from
    msg["To"] = mail_to
    if test:
        msg["Subject"] = "WOMP monitor: testowy e-mail"
        msg.set_content(
            "To jest wiadomość testowa – konfiguracja SMTP działa.\n\n"
            f"Aktualny najbliższy termin: {new_txt}\n"
            f"Zapisany w stanie:          {old_txt}\n\n"
            f"Rezerwacja: {BOOKING_PAGE}\n"
        )
    else:
        msg["Subject"] = f"WOMP: wcześniejszy termin {new:%d.%m.%Y %H:%M}"
        msg.set_content(
            "Zwolnił się wcześniejszy termin badania w Poradni dla Kierowców WOMP.\n\n"
            f"Nowy najbliższy termin:  {new_txt}\n"
            f"Poprzednio widziany:     {old_txt}\n\n"
            f"Rezerwacja: {BOOKING_PAGE}\n\n"
            "Terminy szybko znikają – warto zarezerwować od razu.\n"
        )

    context = ssl.create_default_context()
    if port == 465:  # SSL od początku połączenia (np. Gmail, Onet, WP)
        with smtplib.SMTP_SSL(server, port, context=context, timeout=30) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:  # zwykle 587 – STARTTLS
        with smtplib.SMTP(server, port, timeout=30) as smtp:
            smtp.starttls(context=context)
            smtp.login(user, password)
            smtp.send_message(msg)
    log.info("Wysłano e-mail do %s", mail_to)


# --------------------------------------------------------------------------- #
# Główna logika
# --------------------------------------------------------------------------- #

def main() -> int:
    session = make_session()
    try:
        new_term = get_nearest_term(session)
    except (requests.RequestException, StructureChangedError) as exc:
        log.error("Nie udało się pobrać terminów: %s", exc)
        return 1  # stan pozostaje nietknięty

    old_term = load_state()

    if TEST_EMAIL:
        # Tryb testowy: tylko wysyłka, bez porównania i bez zapisu stanu.
        try:
            send_email(new_term, old_term, test=True)
        except (smtplib.SMTPException, OSError, RuntimeError) as exc:
            log.error("Błąd wysyłki testowego e-maila: %s", exc)
            return 1
        return 0

    log.info(
        "Najbliższy termin: %s | zapisany: %s",
        new_term.strftime(DATE_FMT) if new_term else "brak",
        old_term.strftime(DATE_FMT) if old_term else "brak",
    )

    first_run = not STATE_FILE.exists()
    should_notify = False
    if new_term is not None:
        if old_term is None:
            # Pierwsze uruchomienie albo wcześniej nie było żadnych terminów.
            should_notify = NOTIFY_ON_FIRST_RUN or not first_run
        elif new_term < old_term:
            should_notify = True

    if should_notify:
        try:
            send_email(new_term, old_term)
        except (smtplib.SMTPException, OSError, RuntimeError) as exc:
            # Nie zapisujemy stanu – przy następnym przebiegu spróbujemy ponownie.
            log.error("Błąd wysyłki e-maila: %s", exc)
            return 1
    else:
        log.info("Brak wcześniejszego terminu – mail nie jest wysyłany.")

    # Zapisujemy zawsze bieżący stan ("ostatnio widziany" termin). Dzięki temu,
    # gdy np. 14.01 ktoś zajmie, a później ten termin znów się zwolni,
    # dostaniesz powiadomienie ponownie.
    save_state(new_term)
    return 0


if __name__ == "__main__":
    sys.exit(main())
