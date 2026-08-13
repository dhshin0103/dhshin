#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CRM(crm.mathflat.com) 검색어 필드를 명령줄에서 자동으로 정리하는 도구.
CRM에 API가 없어서 Playwright로 실제 관리자 화면을 직접 조작한다.

최우선 원칙: 오류/데이터 손상 방지.
  - 토큰 단위로만 치환한다. 전체 문자열을 새로 쓰지 않는다.
  - 저장 직전 CRM에서 값을 다시 읽어 원본 토큰이 실제로 있는지 확인하고,
    없으면 건드리지 않고 스킵한다 (누군가 이미 고쳤거나 상황이 달라진 경우).
  - 저장 전 read-back, 저장 후 새로고침+재확인을 모두 거친다.
  - 하나라도 검증에 실패하면 그 즉시 전체 실행을 멈춘다.
  - 결과(대상/원본값/수정값/성공여부)를 audit_log.jsonl에 append-only로 남긴다.
  - 로그인은 절대 대신 하지 않는다 -- login 명령으로 사용자가 직접 로그인한다.

사용법:
  1) 최초 1회 로그인 세션 저장:
       python3 apply_changes.py login
     브라우저 창이 열리면 직접 로그인하고, 터미널로 돌아와 Enter를 누르세요.

  2) 작업 대상 목록 확인:
       python3 apply_changes.py list

  3) 특정 그룹을 먼저 "읽기 전용"으로 점검 (CRM을 절대 수정하지 않음):
       python3 apply_changes.py check --group 1

  4) 점검 결과가 괜찮으면 실제로 적용:
       python3 apply_changes.py apply --group 1

  특정 D코드만 시험해보고 싶으면 --dcode D12345 를 추가로 붙이세요.
"""
import argparse
import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
QUEUE_PATH = os.path.join(SCRIPT_DIR, 'work_queue.json')
AUTH_STATE_PATH = os.path.join(SCRIPT_DIR, 'auth_state.json')
AUDIT_LOG_PATH = os.path.join(SCRIPT_DIR, 'audit_log.jsonl')

BASE_URL = 'https://crm.mathflat.com/customer/{}'
LOGIN_URL = 'https://crm.mathflat.com/'

XPATH_SEARCH_LABEL = "//div[contains(@class,'col1') and normalize-space(text())='검색어']"
XPATH_SEARCH_INPUT = XPATH_SEARCH_LABEL + "/following-sibling::div[contains(@class,'col2')][1]//input"
JS_READ_SEARCH_FIELD = """
() => {
  const col1s = Array.from(document.querySelectorAll('.col1'));
  const target = col1s.find(e => e.textContent.trim() === '검색어');
  const col2 = target ? target.nextElementSibling : null;
  const inp = col2 ? col2.querySelector('input') : null;
  return {
    found: !!inp,
    value: inp ? inp.value : null,
    disabled: inp ? inp.disabled : null,
    readOnly: inp ? inp.readOnly : null,
  };
}
"""


def load_queue():
    with open(QUEUE_PATH, encoding='utf-8') as f:
        return json.load(f)


def append_audit(entry):
    with open(AUDIT_LOG_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def compute_new_value(current, raw, canon, action):
    """토큰 단위 치환. current를 콤마로 split해서 raw와 정확히 일치하는 토큰만
    바꾸거나(rename) 지운다(delete). 나머지 토큰과 순서는 그대로 보존한다.
    바꾼 결과에 중복 토큰이 생기면(예: 이미 표준 표기가 같이 들어있던 경우)
    중복을 제거한다 -- 실제 D59116 레코드에서 발견된 사례를 반영한 안전장치.
    Returns (new_value_or_None, status_string).
    status_string: 'raw_not_found' | 'already_correct' | 'ok' | 'ok_deduped'
    """
    tokens = [t.strip() for t in current.split(',') if t.strip() != '']
    if raw not in tokens:
        return None, 'raw_not_found'

    if action == 'delete':
        new_tokens = [t for t in tokens if t != raw]
    else:
        new_tokens = [canon if t == raw else t for t in tokens]

    seen = set()
    deduped = []
    for t in new_tokens:
        if t not in seen:
            seen.add(t)
            deduped.append(t)

    new_value = ', '.join(deduped)
    if new_value == current:
        return None, 'already_correct'
    status = 'ok_deduped' if len(deduped) != len(new_tokens) else 'ok'
    return new_value, status


def read_search_field(page):
    return page.evaluate(JS_READ_SEARCH_FIELD)


def wait_for_stable_field(page, tries=6, interval=0.4):
    """SPA 라우트 전환 직후에는 필드 값이 아직 이전 페이지 것이거나 비어 있을
    수 있다 (레이스 컨디션 -- D59116 작업 중 실제로 겪은 문제). 연속 두 번
    같은 값이 읽힐 때까지 짧게 재시도해서 안정된 값을 확보한다."""
    prev = None
    for _ in range(tries):
        cur = read_search_field(page)
        if prev is not None and cur == prev:
            return cur
        prev = cur
        time.sleep(interval)
    return prev


def find_save_button(page):
    btn = page.get_by_role('button', name='저장', exact=True).first
    return btn


def process_one(page, item, dcode, dry_run, delay):
    url = BASE_URL.format(dcode)
    page.goto(url, wait_until='networkidle')

    if 'login' in page.url.lower():
        raise RuntimeError('로그인 세션이 만료된 것 같습니다. `python3 apply_changes.py login`을 다시 실행하세요.')

    field = wait_for_stable_field(page)
    entry = {
        'timestamp': now_iso(), 'dcode': dcode, 'group_idx': item['idx'],
        'raw': item['raw'], 'canon': item['canon'], 'action': item['action'],
    }

    if not field.get('found'):
        entry.update(status='skipped_notfound', verified=False)
        append_audit(entry)
        print(f'  [{dcode}] 검색어 필드를 찾지 못함 -- 스킵')
        return 'skipped'

    if field.get('disabled') or field.get('readOnly'):
        entry.update(status='skipped_disabled', old_value=field['value'], verified=False)
        append_audit(entry)
        print(f'  [{dcode}] 필드가 비활성/읽기전용 -- 스킵')
        return 'skipped'

    current = field['value'] or ''
    new_value, calc_status = compute_new_value(current, item['raw'], item['canon'], item['action'])

    if calc_status == 'raw_not_found':
        entry.update(status='skipped_raw_not_found', old_value=current, verified=False)
        append_audit(entry)
        print(f'  [{dcode}] 원본 토큰 "{item["raw"]}"이 현재 값에 없음(이미 처리됐거나 상황이 다름) -- 스킵')
        return 'skipped'

    if calc_status == 'already_correct':
        entry.update(status='skipped_already_correct', old_value=current, verified=True)
        append_audit(entry)
        print(f'  [{dcode}] 이미 올바른 값 -- 스킵')
        return 'skipped'

    print(f'  [{dcode}] {current!r}')
    print(f'       -> {new_value!r}' + (' (중복 제거됨)' if calc_status == 'ok_deduped' else ''))

    if dry_run:
        entry.update(status='dry_run_would_change', old_value=current, new_value=new_value, verified=None)
        append_audit(entry)
        return 'would_change'

    dialog_messages = []
    def on_dialog(dialog):
        dialog_messages.append(dialog.message)
        dialog.dismiss()
    page.on('dialog', on_dialog)

    input_el = page.locator(f'xpath={XPATH_SEARCH_INPUT}')
    input_el.click()
    page.keyboard.press('ControlOrMeta+a')
    input_el.fill(new_value)

    readback = read_search_field(page)
    if readback.get('value') != new_value:
        entry.update(status='failed_typing_mismatch', old_value=current, new_value=new_value,
                      actual=readback.get('value'), verified=False)
        append_audit(entry)
        raise RuntimeError(f'[{dcode}] 입력한 값과 필드 값이 다릅니다 (타이핑 불일치). 전체 실행을 중단합니다.')

    save_btn = find_save_button(page)
    save_btn.click()
    page.wait_for_timeout(600)

    if dialog_messages:
        entry.update(status='aborted_dialog', old_value=current, new_value=new_value,
                      dialog_messages=dialog_messages, verified=False)
        append_audit(entry)
        raise RuntimeError(f'[{dcode}] 저장 중 확인창(dialog)이 나타났습니다: {dialog_messages}. 전체 실행을 중단합니다.')

    page.goto(url, wait_until='networkidle')
    verify_field = wait_for_stable_field(page)
    if verify_field.get('value') != new_value:
        entry.update(status='failed_verification', old_value=current, new_value=new_value,
                      actual_after_reload=verify_field.get('value'), verified=False)
        append_audit(entry)
        raise RuntimeError(
            f'[{dcode}] 저장 후 재확인 실패. 기대값={new_value!r}, 실제값={verify_field.get("value")!r}. '
            f'전체 실행을 중단합니다.'
        )

    entry.update(status='success', old_value=current, new_value=new_value, verified=True)
    append_audit(entry)
    print(f'  [{dcode}] 저장 및 검증 완료')
    time.sleep(delay)
    return 'success'


def run_group(item, dcode_filter, dry_run, delay):
    from playwright.sync_api import sync_playwright

    if not os.path.exists(AUTH_STATE_PATH):
        print('로그인 세션이 없습니다. 먼저 `python3 apply_changes.py login`을 실행하세요.')
        sys.exit(1)

    dcodes = item['dcodes']
    if dcode_filter:
        dcodes = [d for d in dcodes if d == dcode_filter]
        if not dcodes:
            print(f'그룹 {item["idx"]}에 D코드 {dcode_filter}가 없습니다.')
            sys.exit(1)

    print(f'\n=== 그룹 {item["idx"]} [{item["group"]}] {item["action"]}: '
          f'{item["raw"]!r} -> {item["canon"]!r} ({len(dcodes)}건) ===')
    print(f'모드: {"점검(읽기전용, CRM 수정 없음)" if dry_run else "실제 적용"}\n')

    counts = {'success': 0, 'would_change': 0, 'skipped': 0}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(storage_state=AUTH_STATE_PATH)
        page = context.new_page()
        try:
            for dcode in dcodes:
                try:
                    result = process_one(page, item, dcode, dry_run, delay)
                    counts[result] = counts.get(result, 0) + 1
                except RuntimeError as e:
                    print(f'\n*** 중단: {e} ***')
                    print(f'지금까지: 성공/변경예정 {counts.get("success",0)+counts.get("would_change",0)}건, '
                          f'스킵 {counts.get("skipped",0)}건')
                    sys.exit(1)
        finally:
            context.close()
            browser.close()

    print(f'\n완료. 성공/변경예정 {counts.get("success",0)+counts.get("would_change",0)}건, '
          f'스킵 {counts.get("skipped",0)}건 (총 {len(dcodes)}건 중)')
    print(f'감사 로그: {AUDIT_LOG_PATH}')


def cmd_login(args):
    from playwright.sync_api import sync_playwright
    print('브라우저 창이 열립니다. CRM에 직접 로그인한 뒤, 이 터미널로 돌아와 Enter를 누르세요.')
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(LOGIN_URL)
        input('로그인을 완료했으면 Enter... ')
        context.storage_state(path=AUTH_STATE_PATH)
        browser.close()
    print(f'로그인 세션을 저장했습니다: {AUTH_STATE_PATH}')


def cmd_list(args):
    queue = load_queue()
    print(f'{"#":>3} {"그룹":15s} {"작업":6s} {"원본 표기":30s} {"통합 표기":25s} {"건수":>5s}  비고')
    for e in queue:
        detail = (e['workdetail'] or '').replace('\n', ' / ')
        print(f'{e["idx"]:3d} {e["group"]:15s} {e["action"]:6s} {e["raw"]:30s} {e["canon"]:25s} '
              f'{len(e["dcodes"]):5d}  {detail}')
    print(f'\n총 {len(queue)}개 그룹, {sum(len(e["dcodes"]) for e in queue)}건')


def cmd_check(args):
    queue = load_queue()
    item = next((e for e in queue if e['idx'] == args.group), None)
    if not item:
        print(f'그룹 {args.group}을 찾을 수 없습니다. `list`로 번호를 확인하세요.')
        sys.exit(1)
    run_group(item, args.dcode, dry_run=True, delay=args.delay)


def cmd_apply(args):
    queue = load_queue()
    item = next((e for e in queue if e['idx'] == args.group), None)
    if not item:
        print(f'그룹 {args.group}을 찾을 수 없습니다. `list`로 번호를 확인하세요.')
        sys.exit(1)
    run_group(item, args.dcode, dry_run=False, delay=args.delay)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='cmd', required=True)

    sub.add_parser('login', help='CRM에 로그인하고 세션을 저장 (최초 1회, 세션 만료 시 재실행)')

    sub.add_parser('list', help='작업 대상 그룹 목록 출력')

    p_check = sub.add_parser('check', help='읽기 전용 점검 (CRM을 절대 수정하지 않음)')
    p_check.add_argument('--group', type=int, required=True, help='work_queue.json의 그룹 번호')
    p_check.add_argument('--dcode', type=str, default=None, help='이 D코드 하나만 점검')
    p_check.add_argument('--delay', type=float, default=0.3, help='건별 대기 시간(초)')

    p_apply = sub.add_parser('apply', help='실제로 CRM에 저장')
    p_apply.add_argument('--group', type=int, required=True, help='work_queue.json의 그룹 번호')
    p_apply.add_argument('--dcode', type=str, default=None, help='이 D코드 하나만 적용')
    p_apply.add_argument('--delay', type=float, default=0.8, help='건별 대기 시간(초)')

    args = parser.parse_args()
    {'login': cmd_login, 'list': cmd_list, 'check': cmd_check, 'apply': cmd_apply}[args.cmd](args)


if __name__ == '__main__':
    main()
