#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
folder_structure_fix.py — выравнивание структуры папок Kerio под структуру источника.

Сценарий после миграции imap_migrator (v1.1.55-59): часть кастомных деревьев
ушла под INBOX/ (фолбэк, потому что Kerio запрещает IMAP CREATE папок в корне),
часть имён нормализована (trailing/неразрывные пробелы). Утилита читает список
папок источника (эталон), список папок назначения, вычисляет расхождения и
выравнивает назначение:

  1. INBOX/<X> или INBOX/<X>'  ->  <X>                    (RENAME, письма на месте)
  2. RENAME запрещён           ->  CREATE <X> + COPY + сверка счётчиков
  3. Родителя в корне нет и IMAP CREATE root denied -> строка "нужен ручной корень"

Режим по умолчанию — dry-run: только печатает план. Применение: --apply.
CSV аккаунтов — тот же формат, что у мигратора (email;password[;name]).

Примеры:
  py -3 folder_structure_fix.py --csv accounts.csv --dst m.technograd.by
  py -3 folder_structure_fix.py --csv accounts.csv --dst m.technograd.by --apply
  py -3 folder_structure_fix.py --single info@technograd.by --password "..." --dst m.technograd.by --apply
"""

import argparse
import base64
import csv
import imaplib
import re
import sys
import time

# ---------------------------------------------------------------- имя<->wire

def quote_imap_mailbox(name):
    name = str(name or "")
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def encode_imap_folder_name(utf8_str):
    """UTF-8 -> IMAP modified UTF-7 (ASCII-пробеги остаются как есть)."""
    if not utf8_str:
        return ""

    def encode_component(component):
        result, run = [], []

        def flush():
            if not run:
                return
            b64 = base64.b64encode("".join(run).encode("utf-16be")).decode("ascii")
            result.append("&" + b64.rstrip("=").replace("/", ",") + "-")
            run.clear()

        for ch in component:
            code = ord(ch)
            if 0x20 <= code <= 0x7E and ch != "&":
                flush()
                result.append(ch)
            elif ch == "&":
                flush()
                result.append("&-")
            else:
                run.append(ch)
        flush()
        return "".join(result)

    return "/".join(encode_component(part) for part in utf8_str.split("/"))


def decode_imap_folder_name(encoded_str):
    if not encoded_str:
        return ""
    text = encoded_str.strip('"')

    def repl(match):
        body = match.group(1).replace(",", "/")
        body += "=" * (-len(body) % 4)
        try:
            return base64.b64decode(body).decode("utf-16be")
        except Exception:
            return match.group(0)

    parts = []
    for part in text.split("/"):
        parts.append(re.sub(r"&([A-Za-z0-9+,]+)-", repl, part).replace("&-", "&"))
    return "/".join(parts)


def extract_folder_name(folder_item):
    """Достаёт имя папки из строки LIST (yandex/kerio)."""
    if isinstance(folder_item, (tuple, list)):
        values = [v for v in folder_item if v not in (None, b"", "")]
    else:
        values = [folder_item]
    texts = []
    for value in values:
        if isinstance(value, bytes):
            texts.append(value.decode("utf-8", errors="ignore").strip())
        else:
            texts.append(str(value or "").strip())
    texts = [t for t in texts if t]
    if not texts:
        return "", ""
    text = texts[-1]
    quoted = list(re.finditer(r'"((?:\\.|[^"\\])*)"', text))
    delimiter = ""
    if len(quoted) >= 2:
        cand = re.sub(r"\\(.)", r"\1", quoted[-2].group(1))
        if len(cand) == 1:
            delimiter = cand
    if quoted and not text[quoted[-1].end():].strip():
        raw = re.sub(r"\\(.)", r"\1", quoted[-1].group(1))
    else:
        m = re.search(r"(\S+)$", text)
        raw = m.group(1) if m else text
    if raw.upper() == "NIL":
        return "", delimiter
    return decode_imap_folder_name(raw.strip('"')).strip(), delimiter


def list_imap_folders(connection):
    """Список папок как нормализованные '/"-пути. directory передаем как '\"\"' -
    imaplib.list('', ...) иначе шлёт невалидный 'LIST  *' с двойным пробелом."""
    names = []
    ok = False
    for pattern in ("*", "%"):
        try:
            status, data = connection.list('""', pattern)
        except Exception as error:
            print(f"   [LIST] {pattern}: {error}")
            continue
        if str(status).upper() != "OK":
            continue
        ok = True
        for item in data or []:
            name, delim = extract_folder_name(item)
            if delim and delim != "/":
                name = name.replace(delim, "/")
            if name:
                names.append(name)
    seen, ordered = set(), []
    for name in names:
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            ordered.append(name)
    return ordered


def normalize_ws(folder_name):
    parts = []
    for component in str(folder_name or "").split("/"):
        cleaned = component.replace("\u00a0", " ").replace("\u2007", " ").strip()
        cleaned = re.sub(r" {2,}", " ", cleaned)
        parts.append(cleaned)
    return "/".join(parts)


# ---------------------------------------------------------------- IMAP helpers

def connect(host, port, user, password, require_ssl=True):
    conn = imaplib.IMAP4_SSL(host, port) if require_ssl else imaplib.IMAP4(host, port)
    conn.login(user, password)
    return conn


def connect_destination(host, port, email, password):
    local = email.split("@")[0].strip()
    last_error = None
    for login in (email.strip(), local):
        try:
            return connect(host, port, login, password), login
        except Exception as error:
            last_error = error
    raise last_error


def imap_ok(resp):
    return str(resp or "").upper() == "OK"


def create_folder(connection, folder_name):
    """CREATE цепочкой; Kerio отвечает NO на корневые custom-папки - это ожидаемо."""
    current = ""
    notes = []
    for part in [p for p in folder_name.split("/") if p]:
        current = f"{current}/{part}" if current else part
        encoded = encode_imap_folder_name(current)
        done = False
        for wire in (quote_imap_mailbox(encoded), encoded):
            try:
                res, data = connection.create(wire)
                if imap_ok(res):
                    done = True
                    break
                notes.append(f"CREATE {current}: {res} {data}")
            except Exception as error:
                notes.append(f"CREATE {current}: {error}")
        if done:
            notes.append(f"CREATE {current}: OK")
    return notes


def rename_folder(connection, source, target):
    for a_wire, b_wire in (
        (quote_imap_mailbox(encode_imap_folder_name(source)),
         quote_imap_mailbox(encode_imap_folder_name(target))),
        (encode_imap_folder_name(source), encode_imap_folder_name(target)),
    ):
        try:
            res, data = connection.rename(a_wire, b_wire)
            if imap_ok(res):
                return True, f"OK"
        except Exception as error:
            data = error
    return False, str(data)


def select_readonly(connection, folder_name):
    encoded = encode_imap_folder_name(folder_name)
    for wire in (quote_imap_mailbox(encoded), encoded):
        try:
            res, data = connection.select(wire, readonly=True)
            if imap_ok(res):
                return True
        except Exception:
            continue
    return False


def message_count(connection, folder_name):
    if not select_readonly(connection, folder_name):
        return None
    res, data = connection.search(None, "ALL")
    if not imap_ok(res) or not data:
        return None
    ids = data[0].split()
    return len(ids)


def create_parent_chain(connection, folder_name):
    """Создаёт НЕДОСТАЮЩИХ предков folder_name (не саму папку)."""
    parts = [p for p in folder_name.split("/") if p]
    current = ""
    for part in parts[:-1]:
        current = f"{current}/{part}" if current else part
        if select_readonly(connection, current):
            continue
        encoded = encode_imap_folder_name(current)
        created = False
        detail = ""
        for wire in (quote_imap_mailbox(encoded), encoded):
            try:
                res, data = connection.create(wire)
                detail = f"{res} {data}"
                if imap_ok(res):
                    created = True
                    break
            except Exception as error:
                detail = str(error)
        if not created and not select_readonly(connection, current):
            return False, f"parent {current}: {detail}"
    return True, ""


def copy_messages(connection, source, target, search_batch=500):
    """COPY source -> target на одном соединении. Возвращает (скопировано, total)."""
    if not create_parent_chain(connection, target)[0]:
        return None, None
    create_folder(connection, target)
    if not select_readonly(connection, source):
        return None, None
    res, data = connection.search(None, "ALL")
    if not imap_ok(res) or not data:
        return None, None
    ids = data[0].split()
    total = len(ids)
    if total == 0:
        return 0, 0
    encoded_target = encode_imap_folder_name(target)
    target_wires = [quote_imap_mailbox(encoded_target), encoded_target]
    copied = 0
    for start in range(0, total, search_batch):
        chunk = ids[start:start + search_batch]
        id_set = b",".join(chunk)
        ok_copy = False
        for target_wire in target_wires:
            try:
                res_c, data_c = connection.copy(id_set, target_wire)
                if imap_ok(res_c):
                    ok_copy = True
                    break
            except Exception:
                continue
        if not ok_copy:
            return copied, total
        copied += len(chunk)
    return copied, total


# ---------------------------------------------------------------- логика

SYSTEM_ROOT_HINTS = {
    "inbox", "sent", "sent items", "sent messages", "drafts", "trash",
    "deleted items", "deleted messages", "junk", "junk e-mail", "spam",
    "outbox", "archive", "archives", "корзина", "спам", "черновики",
    "отправленные", "удалённые", "удаленные",
}


def is_system_folder(name):
    return name.strip().casefold() in SYSTEM_ROOT_HINTS


def plan_and_fix(account, password, args):
    """Возвращает список строк-отчёта для CSV и печатает план/действия."""
    email = account.strip()
    rows = []
    print(f"\n=== {email} ===")
    try:
        src = connect(args.src, 993, email, password)
    except Exception as error:
        print(f"  ! источник недоступен: {error}")
        return [(email, "", "connect", "ERROR", f"source: {error}")]
    try:
        dst, dst_login = connect_destination(args.dst, 993, email, password)
    except Exception as error:
        print(f"  ! kerio недоступен: {error}")
        src.logout()
        return [(email, "", "connect", "ERROR", f"destination: {error}")]
    rows.append((email, "", "connect", "OK", f"kerio login: {dst_login}"))

    try:
        src_folders = list_imap_folders(src)
        dst_folders = list_imap_folders(dst)
    finally:
        # соединения живут до конца обработки аккаунта
        pass

    # эталон: имена источника после тех же нормализаций, что применяет мигратор
    expected = {}
    for name in src_folders:
        target = normalize_ws(name.replace("|", "/"))
        expected.setdefault(target.casefold(), target)

    actual = {name.casefold(): name for name in dst_folders}

    todo_moves = []  # (src_path_on_kerio, target_path)
    manual_roots = set()
    missing_create = set()

    def root_of(path):
        return path.split("/")[0] if "/" in path else path

    for target_key in sorted(expected, key=lambda k: (k.count("/"), k)):
        target = expected[target_key]
        if is_system_folder(target):
            continue
        if target_key in actual:
            continue
        inbox_variant = f"INBOX/{target}"
        if inbox_variant.casefold() in actual:
            todo_moves.append((actual[inbox_variant.casefold()], target))
        else:
            missing_create.add(target)

    if not todo_moves and not missing_create:
        print("  структура совпадает, действий не требуется")

    for src_path, target in sorted(todo_moves, key=lambda item: item[1].count("/")):
        print(f"  MOVE  {src_path}\t-> {target}")
        rows.append((email, target, f"rename {src_path} -> {target}", "PLAN" if args.dry_run else "", ""))

    for target in sorted(missing_create, key=lambda t: (t.count("/"), t.casefold())):
        root = root_of(target)
        print(f"  CREATE/MOVE-absent  {target} (на Kerio нет ни в корне, ни под INBOX)")
        rows.append((email, target, "recreate-folder", "PLAN" if args.dry_run else "", ""))
        if root.casefold() not in actual and not is_system_folder(root):
            manual_roots.add(root)

    for root in sorted(manual_roots, key=str.casefold):
        involved = [target for _, target in todo_moves] + sorted(missing_create)
        if any(target == root or target.startswith(root + "/") for target in involved):
            print(f"  ! корень '{root}' в корне Kerio отсутствует: IMAP CREATE там запрещён -")
            print(f"    создайте папку '{root}' наверху в веб-почте Kerio (1 раз), затем повторите запуск")
            rows.append((email, root, "manual-root", "NEEDED", "create in Kerio webmail"))

    if args.dry_run:
        return rows

    # -------------------------- APPLY --------------------------
    moved_ok, moved_fail, manual_needed = 0, 0, 0
    for _pass in range(4):  # несколько проходов: после переименований сверяем факт
        changed = False
        dst_folders = list_imap_folders(dst)
        actual = {name.casefold(): name for name in dst_folders}
        for src_path, target in sorted(todo_moves, key=lambda item: item[1].count("/")):
            if target.casefold() in actual:
                continue  # уже на месте
            if src_path.casefold() not in actual:
                continue  # источник переехал/исчез (переименован родителем)
            # гарантируем предков цели: сначала через RENAME родителей, затем CREATE
            parts = [p for p in target.split("/") if p]
            parents_ready = True
            for i in range(1, len(parts)):
                parent = "/".join(parts[:i])
                if parent.casefold() in actual:
                    continue
                inbox_parent = f"INBOX/{parent}"
                if inbox_parent.casefold() in actual:
                    ok_r, det = rename_folder(dst, actual[inbox_parent.casefold()], parent)
                    if ok_r:
                        print(f"    RENAME {inbox_parent} -> {parent}: OK")
                        dst_folders = list_imap_folders(dst)
                        actual = {n.casefold(): n for n in dst_folders}
                        changed = True
                    else:
                        ok_c, det_c = create_parent_chain(dst, parent + "/._probe")
                        if ok_c and select_readonly(dst, parent):
                            changed = True
                        else:
                            parents_ready = False
                            print(f"    ! предок {parent}: {det} / {det_c}")
                else:
                    ok_c, det_c = create_parent_chain(dst, parent + "/._probe")
                    if ok_c and select_readonly(dst, parent):
                        changed = True
                    else:
                        parents_ready = False
                        print(f"    ! предок {parent} не создан: {det_c}")
                if not parents_ready:
                    break
            if not parents_ready:
                continue

            ok, detail = rename_folder(dst, src_path, target)
            if not ok and args.copy_fallback:
                copied, total = copy_messages(dst, src_path, target)
                if total is not None and copied == total:
                    ok, detail = True, f"copy {copied}/{total}"
                else:
                    detail = f"rename: {detail}; copy: {copied}/{total}"
            if ok:
                print(f"    OK   {src_path} -> {target} ({detail})")
                rows.append((email, target, f"rename/copy from {src_path}", "OK", detail))
                moved_ok += 1
                changed = True
                # RENAME родителя уносит и детей: сразу перечитываем факт,
                # чтобы не пытаться переименовывать уже переехавшие папки.
                dst_folders = list_imap_folders(dst)
                actual = {n.casefold(): n for n in dst_folders}
            else:
                print(f"    FAIL {src_path} -> {target}: {detail}")
                rows.append((email, target, f"rename {src_path}", "FAIL", detail))
                moved_fail += 1
        if not changed:
            break

    # недостающие пустые папки (в источнике были, на kerio отсутствуют полностью)
    dst_folders = list_imap_folders(dst)
    actual = {n.casefold(): n for n in dst_folders}
    for target in sorted(missing_create, key=lambda t: (t.count("/"), t.casefold())):
        if target.casefold() in actual:
            continue
        notes = create_folder(dst, target)
        if select_readonly(dst, target):
            print(f"    CREATED {target}")
            rows.append((email, target, "recreate-folder", "OK", ""))
        else:
            last = "; ".join(n for n in notes if "OK" not in n)
            root = target.split("/")[0]
            if root.casefold() not in actual and not is_system_folder(root):
                print(f"    MANUAL create {target}: {last}")
                print(f"      -> корень '{root}' отсутствует: IMAP CREATE запрещён - создайте папку '{root}' наверху в веб-почте Kerio (1 раз), затем повторите запуск")
                rows.append((email, root, "manual-root", "MANUAL", "create root in Kerio webmail, then rerun"))
                manual_needed += 1
            else:
                print(f"    FAIL create {target}: {last}")
                rows.append((email, target, "recreate-folder", "FAIL", last))
                moved_fail += 1

    print(f"  итог: перенесено {moved_ok}, ошибок {moved_fail}, ждёт ручного корня {manual_needed}")
    try:
        src.logout()
    except Exception:
        pass
    try:
        dst.logout()
    except Exception:
        pass
    return rows


def load_accounts(args):
    accounts = []
    if args.single:
        if not args.password:
            raise SystemExit("--single требует --password")
        accounts.append((args.single.strip(), args.password))
    elif args.csv:
        with open(args.csv, newline="", encoding="utf-8-sig") as csv_file:
            for row in csv.reader(csv_file, delimiter=args.delimiter):
                if not row or not row[0].strip() or row[0].strip().startswith("#"):
                    continue
                if len(row) < 2:
                    continue
                accounts.append((row[0].strip(), row[1].strip()))
    else:
        raise SystemExit("укажите --csv accounts.csv или --single email --password pwd")
    return accounts


def main():
    parser = argparse.ArgumentParser(
        description="Выравнивание структуры папок Kerio под структуру источника (post-migration)."
    )
    parser.add_argument("--csv", help="CSV со столбцами email;password (как у мигратора)")
    parser.add_argument("--single", help="email одного аккаунта")
    parser.add_argument("--password", help="пароль для --single")
    parser.add_argument("--src", default="imap.yandex.ru", help="IMAP источника (структура-эталон)")
    parser.add_argument("--dst", default="m.technograd.by", help="IMAP назначения (Kerio)")
    parser.add_argument("--delimiter", default=";", help="разделитель CSV (по умолчанию ';')")
    parser.add_argument("--apply", action="store_true", help="применить изменения (без флага - dry-run)")
    parser.add_argument("--copy-fallback", action="store_true",
                        help="если RENAME отказан: CREATE+COPY сообщений со сверкой счётчиков")
    parser.add_argument("--report", default="structure_fix_report.csv", help="куда писать отчёт")
    args = parser.parse_args()
    args.dry_run = not args.apply

    accounts = load_accounts(args)
    print(f"Аккаунтов: {len(accounts)} | режим: {'DRY-RUN (только план)' if args.dry_run else 'APPLY'}")
    all_rows = []
    for email, password in accounts:
        try:
            all_rows.extend(plan_and_fix(email, password, args))
            time.sleep(0.3)
        except Exception as error:
            print(f"  !! {email}: {error}")
            all_rows.append((email, "", "account", "ERROR", str(error)))

    with open(args.report, "w", newline="", encoding="utf-8-sig") as report_file:
        writer = csv.writer(report_file, delimiter=";")
        writer.writerow(["account", "folder", "action", "status", "detail"])
        writer.writerows(all_rows)
    print(f"\nОтчёт: {args.report}")


if __name__ == "__main__":
    sys.exit(main())
