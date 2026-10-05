"""Authenticated, read-only AFSer acquisition and fail-closed projections.

Source values are processed locally and never logged. Nationwide source records
are preserved privately, including inactive entries, while the map gets only
active, anonymous fields. Sending pins come from the real signup task board.
"""

import hashlib
import http.cookiejar
import json
import os
import re
import sqlite3
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

from .config import load_json, private_write
from .geodata import ATTRIBUTION, GeoData, chapter_id
from .models import Chapter, RawRecord, Record, SourceSnapshot
from .source import IncompleteSource, RestrictedRedirects, SessionExpired, SourceError

BASE = "https://www.afser.de"
TASKS = frozenset({"getAllChapters", "getAllRegions", "getAllStudents", "getAllFwdStudents", "getAllFamiliesWithStudents", "getHostingPotentialFamilies", "getAllHostingStudentProfiles", "getAllInterviews"})
REQUIRED_TASKS = ("getAllChapters", "getAllRegions", "getAllStudents", "getAllFwdStudents", "getAllFamiliesWithStudents", "getHostingPotentialFamilies", "getAllHostingStudentProfiles")
ACTIVE_FUTURE = frozenset({"Admission", "Preparation"})
INACTIVE = frozenset({"Termination", "Returned", "Program End", "Cancelled", "Canceled", "Withdrawn", "Rejected", "Retired"})
VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})


class Node:
    """Small private HTML tree; text never enters logs or exception messages."""
    def __init__(self, tag="document", attrs=None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.parent = parent
        self.children = []

    def all(self, tag=None, **attrs):
        for child in self.children:
            if not isinstance(child, Node):
                continue
            if (tag is None or child.tag == tag) and all(child.attrs.get(k) == v for k, v in attrs.items()):
                yield child
            yield from child.all(tag, **attrs)

    def text(self):
        return " ".join((c.text() if isinstance(c, Node) else c) for c in self.children).strip()

    def classes(self):
        return set(self.attrs.get("class", "").split())


class Document(HTMLParser):
    def __init__(self, content: str):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack = self.stack[:index]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_date(value) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    for format in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value.strip()[:19], format).date()
        except ValueError:
            pass
    return None


def schema(value, prefix="", depth=0) -> set[str]:
    result = set()
    if depth > 6:
        return result
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else key
            result.add(path)
            result.update(schema(child, path, depth + 1))
    elif isinstance(value, list):
        for child in value:
            result.update(schema(child, prefix + "[]", depth + 1))
    return result


def source_identity(payload, prefix: str, index=0) -> str:
    if isinstance(payload, dict) and payload.get("Id"):
        return str(payload["Id"])
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return prefix + ":" + hashlib.sha256(encoded).hexdigest()[:24]


class AfserSource:
    def __init__(self, config: dict, private_dir: Path):
        self.config = config
        self.private_dir = Path(private_dir)
        self.cache_dir = self.private_dir / "source-cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cache_dir.chmod(0o700)
        self.cookies = http.cookiejar.MozillaCookieJar(str(self.private_dir / "cookies.txt"))
        if Path(self.cookies.filename).exists():
            if Path(self.cookies.filename).is_symlink():
                raise SourceError()
            try:
                self.cookies.load(ignore_discard=True)
            except (OSError, http.cookiejar.LoadError):
                pass
        self.opener = build_opener(RestrictedRedirects(), HTTPCookieProcessor(self.cookies))
        self.last_request = 0.0
        self.bootstrap = bool(config.get("bootstrapInitial")) and not (self.private_dir / "source-manifest.json").exists()
        self.bootstrap_cache = bool(config.get("bootstrapCache")) and not (self.private_dir / "source-manifest.json").exists()
        self.manifest = {"fetchedAt": datetime.now(timezone.utc).isoformat(), "entities": {}, "geodata": ATTRIBUTION}
        self.geo = GeoData(self.private_dir)
        self.chapter_codes = {}
        self.chapter_postcodes = {}

    def _request(self, url: str, data: bytes | None = None) -> bytes:
        RestrictedRedirects.validate_url(url)
        # POST is reserved exclusively for the existing login form. Acquisition
        # and interview inspection are GET only; signup is done on AFSer by users.
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        allowed = parts.path in {"/", "/index.php", "/plz-suche.html", "/avt/avtprojects.html"} or bool(re.fullmatch(r"/ereignis-liste/avtproject/\d+\.html", parts.path))
        if not allowed:
            raise SourceError()
        if data is not None and parts.path not in {"/", "/index.php"}:
            raise SourceError()
        if parts.path == "/index.php" and data is None:
            if query.get("option") != ["com_participantslist"] or query.get("controller") != ["participantslist"] or query.get("task", [None])[0] not in TASKS:
                raise SourceError()
        headers = {"User-Agent": "AFSER-Private-Map/0.1 (authorized read-only sync)", "Accept": "application/json,text/html"}
        request = Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
        # Retry only reads. Replaying the login POST is never automatic.
        attempts = 3 if data is None else 1
        for attempt in range(attempts):
            elapsed = time.monotonic() - self.last_request
            if elapsed < 0.2:
                time.sleep(0.2 - elapsed)
            retry_delay = 0.5 * 2 ** attempt
            try:
                with self.opener.open(request, timeout=120) as response:
                    body = response.read(32 * 1024 * 1024 + 1)
                if len(body) > 32 * 1024 * 1024:
                    raise IncompleteSource()
                return body
            except HTTPError as error:
                if error.code in {401, 403}:
                    raise SessionExpired() from None
                if error.code not in {408, 429, 500, 502, 503, 504} or attempt + 1 == attempts:
                    raise SourceError() from None
                try:
                    retry_delay = max(retry_delay, min(10, float(error.headers.get("Retry-After", "0"))))
                except (AttributeError, TypeError, ValueError):
                    pass
            except (URLError, TimeoutError, OSError):
                if attempt + 1 == attempts:
                    raise SourceError() from None
            finally:
                self.last_request = time.monotonic()
            time.sleep(retry_delay)

    @staticmethod
    def _login_form(content: str):
        document = Document(content).root
        for form in document.all("form"):
            if any(field.attrs.get("type") == "password" for field in form.all("input")):
                return form
        return None

    def _login(self):
        content = self._request(BASE + "/").decode("utf-8", "replace")
        form = self._login_form(content)
        if form is not None:
            password_path = Path(self.config.get("passwordFile", self.private_dir.parent / ".afser-password")).expanduser()
            if not password_path.exists() or password_path.is_symlink():
                raise SessionExpired()
            password_path.chmod(0o600)
            username = self.config.get("username")
            if not isinstance(username, str) or not username:
                raise SessionExpired()
            fields = {field.attrs["name"]: field.attrs.get("value", "") for field in form.all("input") if field.attrs.get("type") == "hidden" and field.attrs.get("name")}
            if fields.get("task") not in {"user.login", "login"}:
                raise SessionExpired()
            fields.update(username=username, password=password_path.read_text().strip())
            action = urljoin(BASE + "/", form.attrs.get("action", ""))
            content = self._request(action, urlencode(fields).encode()).decode("utf-8", "replace")
            if self._login_form(content) is not None:
                raise SessionExpired()
        cookie_path = Path(self.cookies.filename)
        # MozillaCookieJar creates with the process umask, so create the file
        # owner-only first even when this adapter is called outside the CLI.
        fd = os.open(cookie_path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        cookie_path.chmod(0o600)
        self.cookies.save(ignore_discard=True)

    def _get_html(self, path: str) -> str:
        content = self._request(BASE + path).decode("utf-8", "replace")
        if self._login_form(content) is not None:
            self._login()
            content = self._request(BASE + path).decode("utf-8", "replace")
            if self._login_form(content) is not None:
                raise SessionExpired()
        return content

    @staticmethod
    def _records(response) -> list[dict]:
        if not isinstance(response, dict) or response.get("error"):
            raise IncompleteSource()
        rows = response.get("records")
        if isinstance(rows, str):
            try:
                rows = json.loads(rows)
            except json.JSONDecodeError:
                raise IncompleteSource() from None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise IncompleteSource()
        return rows

    def _task(self, task: str, chapter: str | None = None, filters: dict | None = None):
        if task not in TASKS:
            raise SourceError()
        filters = filters or {}
        if set(filters) - {"status", "programYear"} or any(not isinstance(value, str) or not value or len(value) > 60 for value in filters.values()):
            raise SourceError()
        initial = self.private_dir / "initial" / (task + ".json")
        suffix = "-" + chapter_id(chapter) if chapter is not None else ""
        if filters:
            suffix += "-filter-" + hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()[:16]
        cached = self.cache_dir / (task + suffix + ".json")
        if self.bootstrap_cache and cached.exists() and time.time() - cached.stat().st_mtime < 3600:
            response = load_json(cached)
        elif self.bootstrap and chapter is None and not filters and initial.exists() and time.time() - initial.stat().st_mtime < 24 * 3600:
            response = load_json(initial)
        else:
            params = {"option": "com_participantslist", "controller": "participantslist", "task": task, "format": "json"}
            if chapter is not None:
                params["chapter"] = chapter
            params.update(filters)
            url = BASE + "/index.php?" + urlencode(params)
            raw = self._request(url)
            try:
                response = json.loads(raw)
            except json.JSONDecodeError:
                if self._login_form(raw.decode("utf-8", "replace")) is None:
                    raise IncompleteSource() from None
                self._login()
                try:
                    response = json.loads(self._request(url))
                except json.JSONDecodeError:
                    raise SessionExpired() from None
            private_write(cached, json.dumps(response, ensure_ascii=False))
        self._records(response)
        return response

    def _complete_task(self, task: str, response, chapters: list[dict]) -> list[dict]:
        rows = self._records(response)
        expected = response.get("totalSize")
        if type(expected) is not int or expected < 0:
            raise IncompleteSource()
        if len({source_identity(row, task) for row in rows}) != len(rows):
            raise IncompleteSource()
        if response.get("done") is True and len(rows) == expected:
            return rows
        if task != "getAllStudents":
            raise IncompleteSource()
        # Joomla exposes Salesforce pages incompletely. Its real chapter filter
        # provides complete partitions; verify the union against the nationwide
        # total rather than following an inaccessible Salesforce cursor URL.
        found = {source_identity(row, task): row for row in rows}
        for chapter in chapters:
            code = chapter.get("Chapter_Code__c")
            if not isinstance(code, str) or not code:
                continue
            partition = self._task(task, code)
            part_rows = self._records(partition)
            if partition.get("done") is not True or type(partition.get("totalSize")) is not int or partition["totalSize"] != len(part_rows) or len({source_identity(row, task) for row in part_rows}) != len(part_rows):
                raise IncompleteSource()
            for row in part_rows:
                row_code = (row.get("Chapter__r") or {}).get("Chapter_Code__c")
                if row_code != code:
                    raise IncompleteSource()
                found[source_identity(row, task)] = row
        if len(found) < expected:
            # Some nationwide source entries have no assigned chapter. The
            # AFSer frontend's verified status/year filters cover those too.
            # Reuse the complete chapter partitions and fetch only the remaining
            # slices; their unique union must still equal the nationwide total.
            statuses = sorted({row.get("Status__c") for row in found.values() if isinstance(row.get("Status__c"), str) and row["Status__c"]})
            for status in statuses:
                partition = self._task(task, filters={"status": status})
                part_rows = self._records(partition)
                if any(row.get("Status__c") != status for row in part_rows):
                    raise IncompleteSource()
                found.update({source_identity(row, task): row for row in part_rows})
                if len(found) == expected:
                    break
                if partition.get("done") is not True:
                    years = sorted({str(row["Program_Year__c"]) for row in found.values() if row.get("Program_Year__c") is not None and str(row["Program_Year__c"]).isdigit()})
                    for year in years:
                        sub_partition = self._task(task, filters={"status": status, "programYear": year})
                        sub_rows = self._records(sub_partition)
                        if any(row.get("Status__c") != status or str(row.get("Program_Year__c")) != year for row in sub_rows):
                            raise IncompleteSource()
                        found.update({source_identity(row, task): row for row in sub_rows})
                        if len(found) == expected:
                            break
                if len(found) == expected:
                    break
        if len(found) != expected:
            raise IncompleteSource()
        return list(found.values())

    def _postcode_chapters(self, chapters: list[dict]):
        cache = self.private_dir / "chapter-postcodes.json"
        saved = load_json(cache, {})
        if isinstance(saved, dict) and isinstance(saved.get("fetchedAt"), (int, float)) and time.time() - saved["fetchedAt"] < 24 * 3600:
            mapping = saved.get("mapping", {})
            if isinstance(mapping, dict) and mapping:
                return {k: v for k, v in mapping.items() if v in self.chapter_codes and re.fullmatch(r"\d{5}", k)}
        mapping = {}
        conflicts = set()
        for chapter in chapters:
            code = chapter.get("Chapter_Code__c")
            if not isinstance(code, str) or not code:
                continue
            content = self._get_html("/plz-suche.html?" + urlencode({"view": "plz", "komitee": code}))
            document = Document(content).root
            single = next(document.all("joomla-tab-element", id="plz_single"), None)
            if single is None:
                continue
            for postcode in set(re.findall(r"(?<!\d)\d{5}(?!\d)", single.text())):
                identity = chapter_id(code)
                if postcode in mapping and mapping[postcode] != identity:
                    conflicts.add(postcode)
                else:
                    mapping[postcode] = identity
        for postcode in conflicts:
            mapping.pop(postcode, None)
        private_write(cache, json.dumps({"fetchedAt": time.time(), "mapping": mapping, "conflicts": len(conflicts)}, ensure_ascii=False))
        return mapping

    def _chapter(self, payload, family=False) -> str | None:
        relation = payload.get("Responsible_Chapter__r" if family else "Chapter__r") or {}
        code = relation.get("Chapter_Code__c")
        identity = chapter_id(code) if isinstance(code, str) else None
        return identity if identity in self.chapter_codes else None

    def _locality(self, payload, family=False):
        if family:
            return self.geo.locality(payload.get("Postal_Code__c")) or self.geo.locality(payload.get("Host_Address__c"))
        return self.geo.locality(payload.get("Address__c"))

    @staticmethod
    def _program_dates(payload):
        start = parse_date(payload.get("Program_Start_Date_Overwrite__c")) or parse_date(payload.get("ProgramStartDateDATE__c")) or parse_date(payload.get("Program_Start_Date__c"))
        end = parse_date(payload.get("Program_End_Date_Overwrite__c")) or parse_date(payload.get("ProgramEndDateDATE__c")) or parse_date(payload.get("Program_End_Date__c"))
        return start, end

    @staticmethod
    def _current_program(payload, today):
        status = payload.get("Status__c")
        if status in INACTIVE:
            return False
        start, end = AfserSource._program_dates(payload)
        if start is None or end is None or end < today or end < start:
            return False
        if status == "Participation":
            return start <= today <= end
        return status in ACTIVE_FUTURE and today <= start <= today + timedelta(days=550)

    def _hopee(self, payload, task: str, today):
        chapter = self._chapter(payload)
        if chapter is None or not self._current_program(payload, today):
            return None
        country = self.geo.country((payload.get("Travel_Country__r") or {}).get("Name"))
        name, lat, lon = country if country else (None, None, None)
        # AFSer participant lists currently have no verified record permalink.
        # Carry committee context in the link; AFSer's existing frontend may
        # still use its saved committee selection when the list opens.
        path = "/teilnehmer-innenlisten-sf.html" if task == "getAllFwdStudents" else "/schueler-innenliste-sf.html"
        url = BASE + path + "?" + urlencode({"view": "participantslist", "listKind": "fwdStudents" if task == "getAllFwdStudents" else "sendingStudents", "chapter": self.chapter_codes[chapter]})
        return Record(str(payload["Id"]), "hopees", chapter, True, "active" if payload.get("Status__c") == "Participation" else "pending", url,
                      country=name, latitude=lat, longitude=lon, location_scope="country")

    def _family(self, payload, today, application=None):
        chapter = self._chapter(payload, family=True)
        status = payload.get("Status__c")
        if chapter is None or status in INACTIVE:
            return None
        if application is not None:
            if not self._current_program(application, today) or status != "Participation":
                return None
        elif status not in {"Application", "Admission", "Preparation"}:
            return None
        locality = self._locality(payload, family=True)
        city, lat, lon = locality if locality else (None, None, None)
        path = "/familien-schueler-innenliste-sf.html" if application is not None else "/potentielle-gastfamilien-sf.html"
        kind = "hostingFamiliesWithStudentss" if application is not None else "hostingPotentialFamilies"
        url = BASE + path + "?" + urlencode({"view": "participantslist", "listKind": kind, "chapter": self.chapter_codes[chapter]})
        needed = payload.get("HomeInterview__c") == "Homeinterview durchführen"
        return Record(str(payload["Id"]), "families", chapter, True, "needed" if needed else "active" if status == "Participation" else "pending", url,
                      city=city, latitude=lat, longitude=lon)

    def _hostee(self, application, family, today):
        chapter = self._chapter(family, family=True)
        if chapter is None or not self._current_program(application, today) or family.get("Status__c") != "Participation":
            return None
        locality = self._locality(family, family=True)
        city, lat, lon = locality if locality else (None, None, None)
        country = self.geo.country((application.get("Travel_Country__r") or {}).get("Name"))
        url = BASE + "/familien-schueler-innenliste-sf.html?" + urlencode({"view": "participantslist", "listKind": "hostingFamiliesWithStudentss", "chapter": self.chapter_codes[chapter]})
        return Record(str(application["Id"]), "hostees", chapter, True, "active" if application.get("Status__c") == "Participation" else "pending", url,
                      country=country[0] if country else None, city=city, latitude=lat, longitude=lon)

    def _previous_interviews(self):
        """Compare against a committed import; never date old pickups as new."""
        path = self.private_dir / "afser.sqlite3"
        if not path.exists():
            return {}
        if path.is_symlink():
            raise SourceError()
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute("SELECT value FROM meta WHERE key='active_snapshot'").fetchone()
            if row is None:
                return {}
            snapshot = int(row[0])
            previous = {}
            for (payload,) in db.execute("SELECT payload FROM raw_records WHERE snapshot=? AND entity_type='avtProject'", (snapshot,)):
                item = json.loads(payload)
                if "availableInterviewRoles" in item:
                    previous[str(item["Id"])] = {"free": len(item["availableInterviewRoles"])}
            for (body,) in db.execute("SELECT body FROM public_records WHERE snapshot=? AND kind='sending'", (snapshot,)):
                item = json.loads(body)
                match = re.search(r"/avtproject/(\d+)\.html$", urlsplit(item.get("sourceUrl", "")).path)
                if match and item.get("pickedAt"):
                    previous.setdefault(match[1], {})["pickedAt"] = item["pickedAt"]
            return previous

    @staticmethod
    def _assigned_interview_roles(detail):
        assigned = 0
        for owner in detail.all():
            if not ({"owner", "assignedowner"} & owner.classes()):
                continue
            parent, role, active = owner, None, True
            while parent and parent is not detail:
                classes = parent.classes()
                if any(name.startswith("state_") for name in classes) and "state_1" not in classes:
                    active = False
                if "row" in classes and role is None:
                    headings = list(parent.all("h4"))
                    role = " ".join(h.text() for h in headings) if headings else parent.text()
                parent = parent.parent
            if not active or not role or not re.search(r"homeinterview", role, re.I):
                continue
            if "assignedowner" in owner.classes():
                assigned += 1
                continue
            # Actual board slots are direct spans inside .taskowner.owner.
            # Empty signup controls and nested forms are not confirmed owners.
            assigned += sum(isinstance(node, Node) and node.tag == "span" and
                            "freeowner" not in node.classes() and bool(node.text().strip())
                            for node in owner.children)
        return assigned

    def _board(self, today):
        previous = self._previous_interviews()
        # Empty scalar filters really clear Joomla's saved checkbox filters;
        # this was verified against the live controls. State 1 is published.
        # Keeping all filters in every page request prevents the user's current
        # chapter/search preferences from silently truncating nationwide data.
        parameters = [("type", ""), ("filter[search]", ""), ("filter[distance]", ""),
                      ("filter[state][]", "1"), ("filter[programms]", ""),
                      ("filter[templates]", ""), ("filter[komitee]", "")]
        pending = {0}
        visited = set()
        found = set()
        records = []
        expected_pages = 1
        stats = Counter()
        empty_pages = 0
        while pending:
            offset = min(pending)
            pending.remove(offset)
            if offset in visited or offset < 0 or offset > 100000:
                raise IncompleteSource()
            visited.add(offset)
            path = "/avt/avtprojects.html?" + urlencode(parameters + [("start", str(offset))])
            # Start a fresh board page sequence: mixing a cached first page with
            # live pagination can hide newly created/deleted signup projects.
            content = self._get_html(path)
            private_write(self.cache_dir / f"avt-board-{offset}.html", content)
            document = Document(content).root
            selected_states = [n.attrs.get("value") for n in document.all("input") if n.attrs.get("name") == "filter[state][]" and "checked" in n.attrs]
            if selected_states != ["1"]:
                raise IncompleteSource()
            if any("checked" in n.attrs for n in document.all("input") if n.attrs.get("name") in {"filter[programms][]", "filter[templates][]", "filter[komitee][]"}):
                raise IncompleteSource()
            pagination = next((n for n in document.all("div") if "pagination" in n.classes()), None)
            match = re.search(r"Seite\s+(\d+)\s+von\s+(\d+)", document.text())
            if match:
                expected_pages = max(expected_pages, int(match[2]))
            if pagination:
                for anchor in pagination.all("a"):
                    href = anchor.attrs.get("href", "")
                    parts = urlsplit(urljoin(BASE, href))
                    if parts.path != "/avt/avtprojects.html" or parts.hostname != "www.afser.de":
                        continue
                    starts = parse_qs(parts.query).get("start", [])
                    if starts and starts[0].isdigit():
                        next_offset = int(starts[0])
                        if next_offset not in visited:
                            pending.add(next_offset)
            rows = list(document.all("tr"))
            hidden = {}
            for row in rows:
                for name in row.classes():
                    match = re.fullmatch(r"expand-(\d+)", name)
                    if match:
                        hidden[match[1]] = row
            page_count = 0
            for row in rows:
                cells = [node for node in row.children if isinstance(node, Node) and node.tag == "td"]
                # Project owners get an additional seventh action column.
                if len(cells) < 6:
                    continue
                cells = cells[:6]
                anchor = next((a for a in cells[1].all("a") if re.fullmatch(r"/ereignis-liste/avtproject/\d+\.html", urlsplit(a.attrs.get("href", "")).path)), None)
                if anchor is None:
                    raise IncompleteSource()
                source_path = urlsplit(anchor.attrs["href"]).path
                project_id = re.search(r"/(\d+)\.html$", source_path)[1]
                if project_id in found:
                    raise IncompleteSource()
                found.add(project_id)
                page_count += 1
                stats["publishedProjects"] += 1
                title = cells[1].text()
                detail = hidden.get(project_id)
                fields = {"Id": project_id, "title": title, "chapter": cells[2].text(),
                          "program": cells[3].text(), "dateText": cells[4].text(),
                          "priority": cells[0].text(), "team": cells[5].text(), "sourceUrl": BASE + source_path}
                normalized = None
                if re.search(r"sending[\s-]*homeinterview", title, re.I) and not re.search(r"\b(?:test|dummy|demo)\b", title, re.I):
                    stats["sendingProjects"] += 1
                    if detail is None:
                        raise IncompleteSource()
                    free_roles = []
                    for link in detail.all("a"):
                        href = link.attrs.get("href", "")
                        if not re.fullmatch(r"/(?:avt|ereignis-liste)/avttaskform/addOwner/\d+\.html", urlsplit(href).path):
                            continue
                        parent = link.parent
                        role = None
                        active = True
                        owner_slot = False
                        while parent and parent is not detail:
                            classes = parent.classes()
                            owner_slot = owner_slot or "freeowner" in classes
                            if any(name.startswith("state_") for name in classes) and "state_1" not in classes:
                                active = False
                            if "row" in classes and role is None:
                                headings = list(parent.all("h4"))
                                # Full project pages use h4 role names; the board
                                # expansion uses the same role text in a div.
                                role = " ".join(h.text() for h in headings) if headings else parent.text()
                            parent = parent.parent
                        if owner_slot and active and role and re.search(r"homeinterview", role, re.I):
                            free_roles.append(href)
                    fields["availableInterviewRoles"] = free_roles
                    assigned_roles = self._assigned_interview_roles(detail)
                    fields["assignedInterviewRoles"] = assigned_roles
                    last = previous.get(project_id, {})
                    picked_at = last.get("pickedAt") if assigned_roles else None
                    if assigned_roles and "free" in last and len(free_roles) < last["free"]:
                        picked_at = today.isoformat()
                    if free_roles or assigned_roles:
                        stats["openSendingProjects"] += bool(free_roles)
                        stats["assignedSendingProjects"] += bool(assigned_roles)
                        status = "assigned" if assigned_roles else "open"
                        code_candidates = [identity for identity, code in self.chapter_codes.items()
                                           if re.search(r"(?<![\w])" + re.escape(code) + r"(?![\w])", fields["chapter"], re.I)]
                        if len(code_candidates) != 1:
                            postcode = self.geo.postcode(title)
                            mapped_chapter = self.chapter_postcodes.get(postcode)
                            if mapped_chapter in self.chapter_codes:
                                # This is AFSer's real postal ownership table,
                                # never a nearest-point guess for a committee.
                                code_candidates = [mapped_chapter]
                        if len(code_candidates) == 1:
                            chapter = code_candidates[0]
                            locality = self.geo.locality(title)
                            if locality is None:
                                locality = self._sending_locality(title, chapter)
                            city, lat, lon = locality if locality else (None, None, None)
                            dates = [parse_date(part) for part in re.findall(r"\d{2}\.\d{2}\.\d{4}", fields["dateText"])]
                            dates = [value for value in dates if value]
                            deadline = max(dates) if dates else None
                            explicit_urgent = bool(fields["priority"]) or any("critical" in node.classes() for node in [row, *row.all()])
                            urgent = bool(free_roles) and (explicit_urgent or bool(deadline and deadline <= today + timedelta(days=14)))
                            normalized = Record("avt:" + project_id, "sending", chapter, True, status, BASE + source_path,
                                                urgent=urgent, deadline=deadline.isoformat() if deadline else None,
                                                city=city, latitude=lat, longitude=lon, has_open_roles=bool(free_roles), picked_at=picked_at)
                            stats["locatedSendingProjects"] += locality is not None
                        else:
                            stats["sendingChapterUnresolved"] += 1
                            dates = [parse_date(part) for part in re.findall(r"\d{2}\.\d{2}\.\d{4}", fields["dateText"])]
                            dates = [value for value in dates if value]
                            deadline = max(dates) if dates else None
                            explicit_urgent = bool(fields["priority"]) or any("critical" in node.classes() for node in [row, *row.all()])
                            urgent = bool(free_roles) and (explicit_urgent or bool(deadline and deadline <= today + timedelta(days=14)))
                            # Explicitly unknown, never assigned to a guessed
                            # chapter or coordinate. The All view can still show
                            # the anonymous source signup link as an unlocated row.
                            normalized = Record("avt:" + project_id, "sending", "unassigned", True, status, BASE + source_path,
                                                urgent=urgent, deadline=deadline.isoformat() if deadline else None,
                                                has_open_roles=bool(free_roles), picked_at=picked_at)
                records.append(RawRecord(project_id, "avtProject", fields, normalized))
            if not page_count:
                # AFSer's published filter can leave an empty trailing page in
                # the unfiltered pagination count. It is still traversed and
                # retained; never silently stop before the remaining pages.
                empty_pages += 1
            # Entire original source HTML, including private hidden task details,
            # is kept locally so none of the available information is discarded.
            records.append(RawRecord(str(offset), "avtBoardPage", {"path": path, "html": content}))
            if len(visited) > 10000:
                raise IncompleteSource()
        if len(visited) != expected_pages:
            raise IncompleteSource()
        self.manifest["entities"]["avtBoard"] = {"complete": True, "scope": "published nationwide projects", "pages": len(visited), "emptyPages": empty_pages, **dict(stats),
                                                  "sourceLink": "verified project details with signup controls"}
        return records

    def _sending_locality(self, title, chapter):
        # Some project titles omit the postcode. A unique local match to the
        # full private student payload can recover the postal centroid without
        # exposing a name or asking an external geocoder about that household.
        canonical = " ".join(re.findall(r"\w+", title.casefold()))
        matches = []
        for payload in getattr(self, "sending_students", []):
            if self._chapter(payload) != chapter:
                continue
            first, last = payload.get("First_Name__c"), payload.get("Last_Name__c")
            if not isinstance(first, str) or not isinstance(last, str) or not first or not last:
                continue
            variants = [" ".join(re.findall(r"\w+", (first + " " + last).casefold())),
                        " ".join(re.findall(r"\w+", (last + " " + first).casefold()))]
            if any(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", canonical) for name in variants):
                matches.append(self.geo.locality(payload.get("Address__c")))
        return matches[0] if len(matches) == 1 else None

    def fetch(self) -> SourceSnapshot:
        self._login()
        self.geo.load(download=True)
        payloads = {}
        chapters_response = self._task("getAllChapters")
        chapters_raw = self._complete_task("getAllChapters", chapters_response, [])
        chapters = []
        for payload in chapters_raw:
            code, name = payload.get("Chapter_Code__c"), payload.get("Name")
            if not isinstance(code, str) or not code or not isinstance(name, str):
                raise IncompleteSource()
            identity = chapter_id(code)
            if not identity or identity in self.chapter_codes:
                raise IncompleteSource()
            self.chapter_codes[identity] = code
            chapters.append(Chapter(identity, name))
        payloads["getAllChapters"] = chapters_raw
        for task in REQUIRED_TASKS[1:]:
            response = self._task(task)
            payloads[task] = self._complete_task(task, response, chapters_raw)
        self.sending_students = payloads["getAllStudents"]
        # The historical interviews endpoint has an inaccessible Salesforce page
        # cursor. Retain returned data and explicitly record partial coverage;
        # completeness of the actionable board is checked separately.
        historical = self._task("getAllInterviews")
        payloads["getAllInterviews"] = self._records(historical)
        historical_complete = historical.get("done") is True and historical.get("totalSize") == len(payloads["getAllInterviews"])
        self.chapter_postcodes = self._postcode_chapters(chapters_raw)
        today = datetime.now(timezone.utc).astimezone(__import__("zoneinfo").ZoneInfo("Europe/Berlin")).date()
        records = []
        for task, rows in payloads.items():
            for index, payload in enumerate(rows):
                normalized = self._hopee(payload, task, today) if task in {"getAllStudents", "getAllFwdStudents"} else self._family(payload, today) if task == "getHostingPotentialFamilies" else None
                records.append(RawRecord(source_identity(payload, task, index), task, payload, normalized))
                if task == "getAllFamiliesWithStudents":
                    application, family = payload.get("Application__r") or {}, payload.get("Hosting_Family__r") or {}
                    if application and family:
                        records.append(RawRecord(source_identity(family, "hostingFamily"), "hostingFamily", family, self._family(family, today, application)))
                        records.append(RawRecord(source_identity(application, "hostingApplication"), "hostingApplication", application, self._hostee(application, family, today)))
            self.manifest["entities"][task] = {"count": len(rows), "complete": historical_complete if task == "getAllInterviews" else True,
                                                "done": historical.get("done") if task == "getAllInterviews" else True,
                                                "total": historical.get("totalSize") if task == "getAllInterviews" else len(rows), "schema": sorted(schema(rows))}
        records.extend(self._board(today))
        # Source relations may repeat a family when it hosts two students. Keep
        # each original assignment payload but only one derived entity and one
        # map projection. An active placement wins over a prospective-family
        # projection for the same household identity.
        unique = {}
        for record in records:
            key = (record.entity_type, record.source_id)
            if key not in unique or unique[key].normalized is None:
                unique[key] = record
        records = list(unique.values())
        chosen = {}
        for index, record in enumerate(records):
            if record.normalized:
                key = (record.normalized.kind, record.normalized.source_id)
                previous = chosen.get(key)
                if previous is None or record.entity_type == "hostingFamily":
                    if previous is not None:
                        original = records[previous]
                        records[previous] = RawRecord(original.source_id, original.entity_type, original.payload)
                    chosen[key] = index
                else:
                    records[index] = RawRecord(record.source_id, record.entity_type, record.payload)
        self.manifest["normalisedCounts"] = dict(Counter(record.normalized.kind for record in records if record.normalized))
        self.manifest["locationCoverage"] = {
            kind: {"active": sum(record.normalized is not None and record.normalized.kind == kind for record in records),
                   "located": sum(record.normalized is not None and record.normalized.kind == kind and record.normalized.latitude is not None and record.normalized.longitude is not None for record in records)}
            for kind in ("sending", "hopees", "hostees", "families")}
        self.manifest["postcodeMappings"] = len(self.chapter_postcodes)
        return SourceSnapshot(chapters, records, self.geo.places(self.chapter_postcodes), self.manifest)
