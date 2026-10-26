"""
Files: finding files and folders through the Windows Search index (the one the Start menu and File
Explorer search use), by name and by the words inside them, then opening one or showing it in its folder.
Only what Windows indexes is found: the user folders by default, more under
Settings > Privacy & security > Searching Windows.
"""
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..phrases import words
from ..ui.status import Status

log = logging.getLogger("jarvis")

INDEX = "Provider=Search.CollatorDSO;Extended Properties='Application=Windows';"
COLUMNS = "System.ItemPathDisplay, System.ItemTypeText, System.DateModified"
MAX_RESULTS = 5      # read out at most this many: small models read out every line they're given
LIST_RESULTS = 50    # shown at most in the list to click one of, when there's a window for it
NAME_CANDIDATES = 50   # names fetched at least, to pick the best from: 'cv' is inside many names
MAX_FOLDERS = 5      # 'in downloads' searches at most this many folders of that name
# folders full of files nobody means: hidden ones like '.vscode', program data and code libraries
NOISE = ("\\.", "\\AppData\\", "\\site-packages\\", "\\node_modules\\", "pycache", "\\venv\\", "\\$Recycle.Bin\\")
KINDS = {   # find_files' kind -> what it finds
    "pdf": (".pdf",), "word": (".doc", ".docx"), "excel": (".xls", ".xlsx", ".csv"),
    "powerpoint": (".ppt", ".pptx"), "text": (".txt",),
    "spreadsheet": (".xls", ".xlsx", ".csv", ".ods"), "presentation": (".ppt", ".pptx", ".odp"),
    "document": "document", "picture": "picture", "photo": "picture", "image": "picture",
    "video": "video", "music": "music", "song": "music", "folder": "folder",
}
# file types said by their extension: 'open a png from my downloads'
EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "heic", "mp4", "mov", "avi", "mkv", "mp3", "wav", "flac",
              "doc", "docx", "xls", "xlsx", "csv", "ppt", "pptx", "txt", "zip", "rar", "iso"}
# what speech recognition mishears or splits: 'world file' is a Word file, 'power point' PowerPoint
MISHEARD = [(re.compile(r"\bworld (?=files?\b|documents?\b)"), "word "), (re.compile(r"\bpower point\b"), "powerpoint")]
# filler the model often leaves in a query: 'my tax return file' searches for 'tax return'
FILLER = {"my", "the", "a", "an", "all", "every", "any", "file", "files", "document", "documents", "folder",
          "called", "named", "about", "with", "of", "for", "that", "which", "says", "mentions", "in", "on", "from",
          "please", "me", "spreadsheet", "presentation", "picture", "photo", "image", "video", "song",
          "computer", "pc", "laptop"}
# what 'open ...' is about when it isn't a file: 'open a new tab', 'open youtube in chrome'
NOT_FILES = {"tab", "tabs", "website", "site", "browser", "app", "application", "it", "that", "this", "one"}
# 'find my resume', 'where is the budget file', 'search all the videos in downloads': plainly a file search
FIND = re.compile(r"\b(find|locate|look for|where is|where's|where are|show me|show|list|search for|search)\s+(.+)")
FIND_NEEDS_FILE_WORDS = {"show me", "show", "list", "search for", "search"}   # 'show me the weather' isn't one
FILE_WORDS = {"file", "files", "document", "documents", "folder", "folders", "pdf", "pdfs", "spreadsheet",
              "spreadsheets", "presentation", "presentations", "picture", "pictures", "photo", "photos",
              "image", "images", "video", "videos", "song", "songs", "music"} | EXTENSIONS | {e + "s" for e in EXTENSIONS}
# where the files are: 'my pdfs in downloads', 'the pictures on my desktop', 'files in the cv folder'
IN_FOLDER = re.compile(r"^(.*)\s+(?:in|from|on|inside|under|within)\s+(?:my\s+|the\s+)?(.+?)(?:\s+folder)?$")
PROGRAMS = {".exe", ".msi", ".bat", ".cmd", ".ps1", ".vbs", ".lnk", ".url", ".com", ".scr"}

# how a follow-up like 'open the second one' or 'number twelve' names a result of the last search
UNITS = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}
NUMBERS = {word: n for n, word in enumerate(UNITS, 1)} | TENS
ORDINALS = {word: n for n, word in enumerate(
    ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth", "eleventh",
     "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth", "nineteenth"], 1)}
ORDINALS |= {"twentieth": 20, "thirtieth": 30, "fortieth": 40, "fiftieth": 50, "last": -1}
NUMERAL = re.compile(r"(\d{1,3})(st|nd|rd|th)?")   # '12' or '12th'
THE_ONE = {"it", "that", "this", "that one", "this one", "the file", "that file", "this file"}
OPEN_WORDS = {"open", "start", "play", "launch", "run", "view", "show"}
WHERE_WORDS = {"folder", "where", "where's", "location", "explorer", "directory"}
FOLLOW_UP_WORDS = {"the", "one", "number", "please", "file", "now", "ok", "okay", "yes", "and", "jarvis", "it",
                   "that", "this", "me", "its", "in", "is", "which", "go", "with", "to", "can", "could", "would",
                   "will", "you"}


@dataclass(frozen=True)
class Found:
    path: Path
    type: str               # how Windows names its type: 'Microsoft Word Document', 'File folder'
    modified: datetime | None
    inside: bool = False    # found by the words inside it rather than its name


def _terms(query: str) -> list[str]:
    """The words to search for: 'my tax return file' is ['tax', 'return']. Only letters and digits,
    so nothing in them can break the query."""
    return [t for t in re.findall(r"\w+", query.lower()) if t not in FILLER]


def _name_terms(name: str) -> list[str]:
    """The words of a file's name: like _terms, but 'my documents' keeps 'documents'."""
    return _terms(name) or [t for t in re.findall(r"\w+", name.lower()) if t not in ("my", "the", "a", "an")]


def _kind_condition(kind: str) -> str:
    """The index condition for a kind like 'pdf' or 'picture', or for an extension like 'txt'."""
    kind = kind.lower().strip().lstrip(".").removesuffix("s")   # 'pdfs', 'pictures', '.docx'
    if kind in ("", "file", "any", "all"):
        return ""
    found = KINDS.get(kind) or ((f".{kind}",) if re.fullmatch(r"[a-z0-9]{1,5}", kind) else None)
    if found is None:
        return ""
    if isinstance(found, str):
        return f"System.Kind = '{found}'"
    return "(" + " OR ".join(f"System.FileExtension = '{ext}'" for ext in found) + ")"


def _within(folders: list[Path]) -> str:
    """The index condition for files anywhere inside these folders."""
    return "(" + " OR ".join("SCOPE='file:" + p.as_posix().replace("'", "''") + "'" for p in folders) + ")"


def _search(where: str, order: str, limit: int) -> list[Found]:
    """Files and folders the index has that match an SQL condition, without the noise folders.
    COM has to be set up on each thread that uses it, and the tools run on the voice thread."""
    import comtypes
    import comtypes.client

    comtypes.CoInitialize()
    noise = "".join(f" AND NOT System.ItemPathDisplay LIKE '%{n}%'" for n in NOISE)
    sql = f"SELECT TOP {limit} {COLUMNS} FROM SystemIndex WHERE SCOPE='file:' AND {where}{noise} ORDER BY {order}"
    connection = comtypes.client.CreateObject("ADODB.Connection")
    connection.Open(INDEX)
    try:
        rows = comtypes.client.CreateObject("ADODB.Recordset")
        rows.Open(sql, connection)
        found = []
        while not rows.EOF:
            path, kind, modified = (rows.Fields.Item(i).Value for i in range(3))
            if modified:   # the index keeps times in UTC
                modified = modified.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
            found.append(Found(Path(path), kind or "file", modified))
            rows.MoveNext()
        rows.Close()
        return found
    finally:
        connection.Close()


def _when(modified: datetime | None) -> str:
    if modified is None:
        return ""
    days = (datetime.now().date() - modified.date()).days
    if days == 0:
        return ", changed today"
    if days == 1:
        return ", changed yesterday"
    year = "" if modified.year == datetime.now().year else f" {modified.year}"
    return f", changed on {modified.day} {modified:%B}{year}"


def _labels(found: list[Found]) -> list[tuple[str, str]]:
    """(name, folder) for each result. A name that comes up twice gets its folder, so the user can
    tell them apart, and a folder named the same as another's gets its parent too, like each game's
    'hardwaresettings'."""
    labels = []
    for f in found:
        name = f.path.name if f.path.is_dir() else f.path.stem
        twins = sum(g.path.name == f.path.name and g.path.parent.name == f.path.parent.name for g in found)
        where = f.path.parent.name or str(f.path.parent)
        if twins > 1 and f.path.parent.parent.name:
            where += f" in {f.path.parent.parent.name}"
        if sum(g.path.stem.lower() == f.path.stem.lower() for g in found) > 1:
            name += f" in {where}"
        labels.append((name, where))
    return labels


def _describe(found: list[Found], query: str) -> str:
    """The results, one a line, so the model knows them for questions about them: name, type, folder,
    when it changed. Only the first few: a long list would fill the model's context."""
    shown = found[:MAX_RESULTS]
    lines = [f"- {name}: {f.type} in {where}{_when(f.modified)}" + (f" (it mentions {query})" if f.inside else "")
             for f, (name, where) in zip(shown, _labels(shown))]
    if len(found) > len(shown):
        lines.append(f"- and {len(found) - len(shown)} more")
    return "\n".join(lines)


def _say_names(found: list[Found], last: str = "and") -> str:
    """The names as Jarvis says them, in the order 'the second one' counts: 'CV Isac Riccardo in
    Engleza, CV Romana actualizat and index'. Not the model: it reorders and drops names."""
    names = [re.sub(r"[_\-]+", " ", name).strip() for name, _ in _labels(found)]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} {last} {names[-1]}"


def _starts_words(name: str, terms: list[str]) -> bool:
    """Whether each term starts a word of the name: 'cv' does in 'CV_Isac_Riccardo' but not in
    'ZxcvbnData'. Words are split at anything but letters and digits, and at capitals too."""
    parts = re.findall(r"[a-z0-9]+", name.lower()) + [p.lower() for p in re.findall(r"[A-Z]?[a-z]+|[A-Z]+|\d+", name)]
    return all(any(p.startswith(t) for p in parts) for t in terms)


def _named(terms: list[str], also: str, limit: int) -> tuple[list[Found], list[Found]]:
    """Files and folders with all the terms in their name, newest first: (those where the terms start
    words of the name, those that only contain them, like 'cv' inside 'ZxcvbnData').
    also: more the files must match, like a kind or a folder."""
    where = " AND ".join(f"System.FileName LIKE '%{t}%'" for t in terms) + (f" AND {also}" if also else "")
    found = _search(where, "System.DateModified DESC", max(NAME_CANDIDATES, 2 * limit))
    starts = [f for f in found if _starts_words(f.path.name, terms)]
    return starts, [f for f in found if f not in starts]


def _by_name(name: str, limit: int) -> list[Found]:
    """Files and folders named like this, newest first: the terms must start words of the name, and
    no programs, so a misheard app name never runs some installer from Downloads."""
    terms = _name_terms(name)
    if not terms:
        return []
    return [f for f in _named(terms, "", limit)[0] if f.path.suffix.lower() not in PROGRAMS][:limit]


def _folders(name: str) -> list[Path]:
    """The folders 'downloads', 'my desktop' or 'cv' means: those named exactly that, or else those whose
    name has the words as words. Empty if there's none."""
    terms = _name_terms(name)
    if not terms:
        return []
    where = "System.Kind = 'folder' AND " + " AND ".join(f"System.FileName LIKE '%{t}%'" for t in terms)
    found = [f.path for f in _search(where, "System.DateModified DESC", NAME_CANDIDATES)
             if f.path.suffix.lower() not in (".lnk", ".url")]   # the Downloads shortcut isn't the folder
    exact = [p for p in found if words(p.name) == " ".join(terms)] or [p for p in found if _starts_words(p.name, terms)]
    # the user's own, not also the empty ones Windows keeps for new users (C:\Users\Default\Downloads)
    return ([p for p in exact if p.is_relative_to(Path.home())] or exact)[:MAX_FOLDERS]


def _number(text: str) -> int | None:
    """The result a follow-up names: 'the second one' 2, 'the twenty first' 21, 'the 12th one' 12,
    'number three' 3, '4' 4, 'the last one' -1."""
    said = words(text).split()
    for i, word in enumerate(said):
        before = said[i - 1] if i else ""
        after = said[i + 1] if i + 1 < len(said) else ""
        if word in ORDINALS:
            n = ORDINALS[word]
            return TENS.get(before, 0) + n if 0 < n < 10 else n   # 'twenty first'
        numeral = NUMERAL.fullmatch(word)
        if numeral and (numeral.group(2) or len(said) == 1 or before == "number"):
            return int(numeral.group(1))
        if word in NUMBERS and (len(said) == 1 or before == "number"):
            n = NUMBERS[word]
            return n + NUMBERS[after] if word in TENS and NUMBERS.get(after, 10) < 10 else n   # 'number twenty one'
    return None


def _only_picks(said: list[str]) -> bool:
    """Whether these words do nothing but pick a result: 'the twelfth one', 'number 3', 'the last one'."""
    return all(w in FOLLOW_UP_WORDS or w in ORDINALS or w in NUMBERS or NUMERAL.fullmatch(w) for w in said)


def _heard(request: str) -> str:
    """The request in words() form, with what speech recognition mishears put right."""
    said = words(request)
    for wrong, right in MISHEARD:
        said = wrong.sub(right, said)
    return said


def _parts(said: str) -> tuple[str, str, str]:
    """(name, kind, folder) in a request for files: 'my pdfs in downloads' is ('my', 'pdfs', 'downloads'),
    'a png from my desktop' ('a', 'png', 'desktop'), 'my cv' ('my cv', '', '')."""
    in_folder = IN_FOLDER.match(said)
    name, folder = (in_folder.group(1), in_folder.group(2)) if in_folder else (said, "")
    kind = next((w for w in name.split() if w.removesuffix("s") in KINDS or w.removesuffix("s") in EXTENSIONS), "")
    return " ".join(w for w in name.split() if w != kind), kind, folder


def _the_one(found: list[Found], name: str) -> Found | None:
    """The file a name clearly means: the only one found, or the only one named exactly that."""
    exact = [f for f in found if words(f.path.stem) == words(name) or words(f.path.name) == words(name)]
    # 'downloads': the Downloads folder, not also the shortcut to it
    exact = [f for f in exact if f.path.suffix.lower() not in (".lnk", ".url")] or exact
    return (exact or found)[0] if len(found) == 1 or len(exact) == 1 else None


def _show_in_folder(path: Path) -> None:
    subprocess.Popen(f'explorer /select,"{path}"')


def _folder(path: Path) -> str:
    """Where a file is, as the list shows it: from the user's own folder down, like 'Downloads'."""
    try:
        return str(path.parent.relative_to(Path.home())) or str(path.parent)
    except ValueError:
        return str(path.parent)


class FileSearch:
    def __init__(self):
        self.screen = Status()   # where a list of files to click one of is shown; this one shows nothing
        self.results: list[Found] = []   # the last search's, for 'open the second one'
        self.chosen: Found | None = None   # the last file opened or shown, for 'where is it?'
        self.asking = "open"   # what Jarvis asked the user to pick a file for: 'open' or 'show'
        # what Jarvis says itself about the last call, as (sentence, its values), or None for the model
        # to word the result, like a failure; set again by every call
        self.reply: tuple[str, dict] | None = None

    @property
    def limit(self) -> int:
        """How many files a search finds at most: a list on screen takes many, reading out only a few."""
        return LIST_RESULTS if self.screen.can_choose else MAX_RESULTS

    def forget(self) -> None:
        """The conversation ended: 'open the second one' next time isn't about these."""
        self.results, self.chosen, self.asking = [], None, "open"

    def find_files(self, query: str = "", kind: str = "", folder: str = "") -> str:
        """Search this PC's files and folders by their name and by the words inside them. Use it when the user asks to find or look for a file, or where one is.

        Args:
          query: Words from the file's name or from what it says, like 'tax return' or 'invoice march'. Leave empty for all files of a kind or in a folder.
          kind: Only if the user names one: 'pdf', 'word', 'excel', 'powerpoint', 'picture', 'video', 'music', 'folder', or an extension like 'png'
          folder: Only if the user names a folder to look in, like 'downloads', 'desktop' or 'cv'
        """
        found = self._lookup(query, kind, folder)
        # looking for a file means wanting to know where it is: picking one shows it in its folder
        return found if isinstance(found, str) else self._present(*found, "show")

    def _lookup(self, query: str, kind: str, folder: str) -> tuple[list[Found], str, str] | str:
        """(the files found, what was looked for like 'pdf files in downloads', the words looked for),
        or what went wrong."""
        kind, folder = (kind or "").strip(), (folder or "").strip()
        singular = kind.lower().removesuffix("s")
        terms = [t for t in _terms(query or "") if t.removesuffix("s") != singular]   # 'pdfs' when kind is pdf
        conditions = [c for c in [_kind_condition(kind)] if c]
        try:
            if folder:
                folders = _folders(folder)
                if folders:
                    conditions.append(_within(folders))
                else:   # 'the document about taxes in 2023': not a folder after all, so words to look for
                    terms += _name_terms(folder)
                    folder = ""
            if not terms and not conditions:
                return "Failed: say what to look for, like a word from the file's name."
            found = self._find(terms, " AND ".join(conditions))
        except Exception as e:
            return f"Failed: Windows Search isn't available ({e}). The Windows Search service may be turned off."
        # 'pdf files for invoices in downloads'
        what = " ".join(filter(None, [singular if conditions and singular else "", "files",
                                      terms and "for " + " ".join(terms), folder and f"in {folder}"]))
        return found, what, " ".join(terms)

    def _present(self, found: list[Found], what: str, said: str, action: str) -> str:
        """Tell the user what a search found. One file is opened or shown in its folder (action 'open'
        or 'show') right away; several go in the list to pick from, or are named without one."""
        if not found:
            self.reply = ("I couldn't find any {what}.", {"what": what})
            return (f"No {what} found. Only folders Windows Search indexes are searched; "
                    "more can be added in Windows Settings, under Privacy and security, Searching Windows.")
        self.results, self.chosen, self.asking = found, None, action
        many = f"the first {len(found)}" if len(found) == self.limit else f"found {len(found)}"
        listed = f"{what.capitalize()}: {many}, in this order:\n{_describe(found, said)}"
        act = self._open if action == "open" else self._show
        one = found[0] if len(found) == 1 else _the_one(found, said) if action == "open" else None
        if one:
            return f"{listed}\n{act(one)}"
        if self._offer(found, action):
            count = "at least {count}" if len(found) == self.limit else "{count}"
            self.reply = ((f"I found {count} files. Pick one from the list.", {"count": len(found)}) if action == "show"
                          else ("Which one? Pick it from the list.", {}))
            return f"{listed}\nThey're shown in a list for the user to pick from."
        if action == "open":
            self.reply = ("Which one: {names}?", {"names": _say_names(found, "or")})
        else:
            self.reply = ("I found {names}.", {"names": _say_names(found)})
        return listed

    def _find(self, terms: list[str], also: str) -> list[Found]:
        """The best ones: files whose name has the terms as words, newest first, then files with them
        inside, best match first, then names that merely contain them. Those weaker matches only top
        the list up to a few, or 'find my cv' would list fifty files that merely mention it. With no
        terms, the newest of all that match also, like a kind or a folder."""
        limit = self.limit
        if not terms:
            return _search(also, "System.DateModified DESC", limit)
        found, contains = _named(terms, also, limit)
        found = found[:limit]
        top_up = max(min(limit, 2 * MAX_RESULTS) - len(found), 0)
        if top_up:
            inside = "CONTAINS(*, '" + " AND ".join(f'"{t}*"' for t in terms) + "')" + (f" AND {also}" if also else "")
            named = {f.path for f in found + contains}
            found += [Found(f.path, f.type, f.modified, inside=True)
                      for f in _search(inside, "System.Search.Rank DESC", top_up * 2) if f.path not in named][:top_up]
        return found + contains[:max(min(limit, 2 * MAX_RESULTS) - len(found), 0)]

    def open_file(self, name: str = "", kind: str = "", folder: str = "") -> str:
        """Open a file or folder with the app it belongs to, like double-clicking it. With only a kind or a folder, like 'open a png from my downloads', the user picks which.

        Args:
          name: The file's name or words from it, like 'tax return' or 'budget.xlsx', or the number of a result of the last search, like '2'
          kind: Only if the user names one, like 'pdf', 'word', 'picture' or 'png'
          folder: Only if the user names the folder it's in, like 'downloads' or 'desktop'
        """
        if (kind or "").strip() or (folder or "").strip():
            found = self._lookup(name, kind, folder)
            return found if isinstance(found, str) else self._present(*found, "open")
        chosen = self._choose(name, "open")
        return chosen if isinstance(chosen, str) else self._open(chosen)

    def _open(self, chosen: Found) -> str:
        try:
            os.startfile(chosen.path)
        except OSError as e:
            return f"Failed to open {chosen.path.name}: {e}"
        self.screen.close_choices()   # picked by voice, or from the list itself
        self.chosen = chosen
        self.reply = ("It's open.", {})
        return f"Opened {chosen.path.name}."

    def _show(self, chosen: Found) -> str:
        _show_in_folder(chosen.path)
        self.screen.close_choices()
        self.chosen = chosen
        self.reply = ("Here it is.", {})
        return f"Showed {chosen.path.name} in its folder, {chosen.path.parent}."

    def _offer(self, found: list[Found], action: str) -> bool:
        """Show the files in a list to click one of, which then opens it or shows it in its folder.
        False when there's no list to show them in (text mode, or no window)."""
        title = f"{len(found)} found, pick one to " + ("open" if action == "open" else "show in its folder")
        items = [(f.path.name, _folder(f.path) + _when(f.modified)) for f in found]
        act = self._open if action == "open" else self._show
        return self.screen.choose(title, items, lambda i: act(found[i]))

    def show_in_folder(self, name: str) -> str:
        """Show where a file is: open its folder in File Explorer with the file selected. Only when the user asks for its folder or location, not to open the file itself.

        Args:
          name: The file's name or words from it, or the number of a result of the last search, like '2'
        """
        chosen = self._choose(name, "show")
        return chosen if isinstance(chosen, str) else self._show(chosen)

    def open_document(self, name: str) -> str | None:
        """For open_app, when no app has this name: 'open my resume' means a file. Opens it if just one
        file or folder has that name; with several, lists them to choose from. None if none does."""
        try:
            found = _by_name(name, self.limit)
        except Exception as e:
            log.warning("Couldn't search the files for %r: %s", name, e)
            return None
        if not found:
            return None
        chosen = self._open_or_ask(found, name, "open")
        return chosen if isinstance(chosen, str) else self._open(chosen)

    def _choose(self, name: str, action: str) -> Found | str:
        """The file or folder name means: a result of the last search by its number, 'it' for the one
        just opened, or the one file the index has by that name. Otherwise what to tell the user;
        several matches become the results to choose from."""
        name = (name or "").strip()
        # '2' or 'the second one', but not a file called 'second draft'
        number = _number(name) if _only_picks(words(name).split()) else None
        if number:
            if not self.results:
                return "Failed: there's no earlier search to choose from. Search for the file first."
            if number > len(self.results):
                return f"Failed: the last search found only {len(self.results)}."
            return self.results[number - 1 if number > 0 else -1]
        if words(name) in THE_ONE or not name:
            if self.chosen or len(self.results) == 1:
                return self.chosen or self.results[0]
            if self.results:   # 'can you open it?' with a list up: pick one, now to open it
                return self._ask_which(self.results, name, action)
            return "Failed: say which file."
        terms = _name_terms(name)
        if not terms:
            return "Failed: say which file."
        among = [f for f in self.results if all(t in words(f.path.name) for t in terms)]
        if len(among) == 1:
            return among[0]
        try:
            found = self._find(terms, "")
        except Exception as e:
            return f"Failed: Windows Search isn't available ({e}). The Windows Search service may be turned off."
        if not found:
            return f"Failed: no file called {name} found."
        return self._open_or_ask(found, name, action)

    def _open_or_ask(self, found: list[Found], name: str, action: str) -> Found | str:
        """The one file meant, if it's clear: the only one, or the only one named exactly that. Otherwise
        the user picks."""
        return _the_one(found, name) or self._ask_which(found, name, action)

    def _ask_which(self, found: list[Found], name: str, action: str) -> str:
        """Ask the user which of these files to open or show: in the list, or by their names."""
        self.results, self.chosen, self.asking = found, None, action
        if self._offer(found, action):
            self.reply = ("Which one? Pick it from the list.", {})
        else:
            self.reply = ("Which one: {names}?", {"names": _say_names(found, "or")})
        return (f"Several files fit, so nothing was done yet. "
                f"The user is asked which one to {action}:\n{_describe(found, name)}")

    @staticmethod
    def asked_to_find(request: str) -> dict | None:
        """find_files' arguments when the request plainly asks for files: 'find my resume', 'where is the
        budget spreadsheet', 'show me all my pdfs in downloads'. Only with a word like 'file' or 'pdf', or
        'my' after find or where, so 'find a good restaurant' or 'show me the weather' is left to the model."""
        match = FIND.search(_heard(request))
        if not match:
            return None
        verb, said = match.group(1), re.sub(r"\s+(please|for me|now)$", "", match.group(2))
        named = set(said.split()) & (FILE_WORDS | (set() if verb in FIND_NEEDS_FILE_WORDS else {"my"}))
        if not named:
            return None
        query, kind, folder = _parts(said)
        return {"query": query, "kind": kind, "folder": folder}

    @staticmethod
    def asked_to_open(request: str, browsers: set[str]) -> str | None:
        """What 'open my resume' or 'can you open a png from my downloads' asks to open, if it could be
        files: not a browser tab, a website or 'it'. None otherwise."""
        match = re.search(r"\bopen (?:up )?(.+)", _heard(request))
        if not match:
            return None
        said = re.sub(r"\s+(please|for me|now)$", "", match.group(1)).strip()
        if set(said.split()) & (NOT_FILES | browsers):
            return None
        return said

    def open_args(self, said: str) -> dict | None:
        """open_file's arguments for what asked_to_open found, if it names files plainly: a kind ('a png'),
        a folder that exists ('from my downloads'), or a file's name. None otherwise."""
        name, kind, folder = _parts(said)
        if kind or (folder and self._exists(_folders, folder)):
            return {"name": name, "kind": kind, "folder": folder}
        return {"name": said} if self._exists(_by_name, said, 1) else None

    @staticmethod
    def _exists(search, *args) -> bool:
        """Whether a search finds anything, and False if Windows Search can't be reached."""
        try:
            return bool(search(*args))
        except Exception as e:
            log.warning("Couldn't search the files for %r: %s", args[0], e)
            return False

    def follow_up(self, request: str) -> tuple[str, dict] | None:
        """The call for a request that plainly picks from the last search: 'open the second one',
        'number twelve', 'show me where it is'. None for anything else, like a new search."""
        if not self.results and not self.chosen:
            return None
        said = words(request).split()
        asked = set(said)
        number = _number(request)
        # 'the second one' on its own does what Jarvis just asked about
        where = bool(asked & WHERE_WORDS) or (self.asking == "show" and not asked & OPEN_WORDS)
        if number:
            if not (asked & (OPEN_WORDS | WHERE_WORDS) or _only_picks(said)):
                return None
            which = str(number if number > 0 else len(self.results))
        elif asked & {"it", "that", "this", "its"} and asked & (OPEN_WORDS | WHERE_WORDS) and \
                asked <= FOLLOW_UP_WORDS | OPEN_WORDS | WHERE_WORDS:   # with several, the user picks which
            which = "it"
        else:
            return None
        return ("show_in_folder" if where else "open_file"), {"name": which}
