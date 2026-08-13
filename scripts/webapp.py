#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CRM D코드(여러 개, 띄어쓰기/콤마/줄바꿈으로 구분)의 검색어 태그를 추가/수정하는 간단한 로컬 웹페이지.
apply_changes.py와 같은 폴더에 두고 쓴다 (같은 로그인 세션, 같은 감사 로그를 공유).

사용법:
  pip install flask
  python3 webapp.py
  -> 브라우저에서 http://127.0.0.1:5050 열기
  -> "로그인" 탭에서 최초 1회(또는 세션 만료 시) CRM 로그인. 터미널 조작 불필요.

세 개의 탭:
  - 로그인: CRM 로그인 세션을 만들거나 다시 만든다. 브라우저 창이 열리면 직접
    로그인하고, 웹페이지의 "로그인 완료" 버튼을 누르면 세션이 저장된다.
  - 태그 추가 / 태그 수정 (mode='add' | 'edit'):
  - 태그 추가: 검색어에 새 태그를 콤마로 이어붙인다. 이미 있으면 "이미 있음"으로 스킵.
  - 태그 수정: 검색어 안에서 기존 태그(정확히 일치하는 토큰)를 찾아 새 태그로 바꾼다.
    해당 D코드에 그 태그가 없으면 "해당 태그 없음"으로 스킵.

동작 방식 (안전을 위해 항상 2단계, 두 모드 공통):
  1) D코드 여러 개(띄어쓰기로 구분) + 태그 정보 입력 -> "확인" -> D코드별로 현재
     검색어 값과, 변경하면 어떻게 바뀌는지 미리보기만 보여준다
     (이 단계에서는 CRM을 절대 수정하지 않음).
  2) 미리보기가 맞으면 "전체 저장" -> D코드를 하나씩 순서대로 실제 저장하고,
     저장 후 다시 읽어서 검증한다. 각 D코드는 독립적으로 처리되어 하나가
     실패해도 다른 D코드에는 영향이 없다 -- 단, 로그인 세션이 만료된 것으로
     보이면 그 순간 남은 D코드 처리를 전부 멈추고 알린다(계속해봐야 다 같은
     이유로 실패할 것이기 때문).
  모든 결과는 audit_log.jsonl에 apply_changes.py와 동일한 형식으로 남는다.
"""
import os
import queue
import re
import secrets
import sys
import threading
import time

from flask import Flask, request, jsonify, abort

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from apply_changes import (
    AUTH_STATE_PATH, BASE_URL, LOGIN_URL, append_audit, now_iso,
    read_search_field, wait_for_stable_field, find_save_button,
    XPATH_SEARCH_INPUT,
)

app = Flask(__name__)

# 이 웹앱은 인증 없이 127.0.0.1에만 열려 있다. 같은 PC의 다른 프로그램이나
# 브라우저에 열린 다른 탭이 /api/*를 몰래 호출해서 CRM을 조작하는 걸 막기 위해,
# 매 실행마다 무작위 토큰을 만들어 페이지에 심어두고 모든 /api/* 요청에서
# 이 토큰을 요구한다 (파일로 저장하지 않음 -- 재시작하면 새로 발급).
API_TOKEN = secrets.token_hex(16)


@app.before_request
def _require_api_token():
    if request.path.startswith('/api/'):
        if request.headers.get('X-Api-Token') != API_TOKEN:
            abort(403)


_lock = threading.Lock()
_state = {'playwright': None, 'browser': None, 'context': None, 'page': None}
_login_state = {'playwright': None, 'browser': None, 'context': None, 'page': None}

# Playwright의 동기 API는 자신을 시작한 OS 스레드에 종속된다. Flask 개발 서버는
# 요청마다 다른 스레드에서 처리할 수 있으므로, 브라우저/페이지 생성과 모든 조작을
# 전용 워커 스레드 하나로 고정해서 실행한다.
_job_queue = queue.Queue()


def _worker_loop():
    while True:
        func, args, kwargs, box = _job_queue.get()
        try:
            box['result'] = func(*args, **kwargs)
        except BaseException as e:
            box['error'] = e
        finally:
            box['event'].set()


threading.Thread(target=_worker_loop, daemon=True).start()


def run_in_worker(func, *args, **kwargs):
    box = {'event': threading.Event()}
    _job_queue.put((func, args, kwargs, box))
    box['event'].wait()
    if 'error' in box:
        raise box['error']
    return box.get('result')


def parse_dcodes(raw):
    """띄어쓰기/콤마/줄바꿈으로 구분된 D코드 목록을 파싱한다.
    중복은 제거하되 처음 등장한 순서를 유지한다."""
    parts = [p.strip() for p in re.split(r'[\s,]+', raw.strip()) if p.strip()]
    seen = set()
    result = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


def compute_add_tag_value(current, tag):
    """current에 tag를 콤마로 이어붙인다. 이미 있으면 추가하지 않는다.
    Returns (new_value_or_None, status)."""
    tokens = [t.strip() for t in current.split(',') if t.strip() != ''] if current else []
    if tag in tokens:
        return None, 'already_present'
    new_tokens = tokens + [tag]
    return ', '.join(new_tokens), 'ok'


def compute_edit_tag_value(current, old_tag, new_tag):
    """current 안에서 old_tag와 정확히 일치하는 토큰을 new_tag로 바꾼다.
    old_tag가 없으면 바꾸지 않는다. 바꾼 결과 new_tag가 중복되면 하나만 남긴다.
    Returns (new_value_or_None, status)."""
    tokens = [t.strip() for t in current.split(',') if t.strip() != ''] if current else []
    if old_tag not in tokens:
        return None, 'not_found'
    if old_tag == new_tag:
        return None, 'no_change'
    new_tokens = []
    seen = set()
    for t in tokens:
        v = new_tag if t == old_tag else t
        if v in seen:
            continue
        seen.add(v)
        new_tokens.append(v)
    return ', '.join(new_tokens), 'ok'


def compute_value(mode, current, params):
    if mode == 'add':
        return compute_add_tag_value(current, params['tag'])
    if mode == 'edit':
        return compute_edit_tag_value(current, params['old_tag'], params['new_tag'])
    raise ValueError(f'unknown mode: {mode}')


def get_page():
    """전역 브라우저/페이지를 지연 생성해서 재사용한다 (요청마다 새로 열지 않음)."""
    if _state['page'] is not None:
        return _state['page']
    if not os.path.exists(AUTH_STATE_PATH):
        raise RuntimeError('로그인 세션이 없습니다. 위쪽 "로그인" 탭에서 먼저 로그인하세요.')
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=False)
    context = browser.new_context(storage_state=AUTH_STATE_PATH)
    page = context.new_page()
    _state.update(playwright=pw, browser=browser, context=context, page=page)
    return page


def load_field(dcode):
    page = get_page()
    url = BASE_URL.format(dcode)
    page.goto(url, wait_until='networkidle')
    if 'login' in page.url.lower():
        raise LoginExpired('로그인 세션이 만료됐습니다. "로그인" 탭에서 다시 로그인하세요.')
    field = wait_for_stable_field(page)
    if not field.get('found'):
        raise RuntimeError(f'{dcode}에서 검색어 필드를 찾지 못했습니다. D코드를 확인해주세요.')
    if field.get('disabled') or field.get('readOnly'):
        raise RuntimeError(f'{dcode}의 검색어 필드가 비활성/읽기전용입니다.')
    return page, field


class LoginExpired(RuntimeError):
    pass


def _reset_main_browser():
    """추가/수정 탭이 쓰던 브라우저를 닫아서, 다음 요청 때 새 로그인 세션으로
    다시 열리게 한다 (로그인을 새로 마친 뒤 호출)."""
    browser = _state.get('browser')
    pw = _state.get('playwright')
    _state.update(playwright=None, browser=None, context=None, page=None)
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            pass


def _login_start():
    if _login_state.get('page') is not None:
        return
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto(LOGIN_URL)
    _login_state.update(playwright=pw, browser=browser, context=context, page=page)


def _login_finish(save):
    page = _login_state.get('page')
    if page is None:
        raise RuntimeError('먼저 "로그인 창 열기"를 눌러주세요.')
    if save:
        _login_state['context'].storage_state(path=AUTH_STATE_PATH)
    browser = _login_state.get('browser')
    pw = _login_state.get('playwright')
    _login_state.update(playwright=None, browser=None, context=None, page=None)
    browser.close()
    pw.stop()
    if save:
        _reset_main_browser()


@app.route('/api/login/status')
def api_login_status():
    exists = os.path.exists(AUTH_STATE_PATH)
    saved_at = None
    if exists:
        saved_at = time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(AUTH_STATE_PATH)))
    return jsonify(exists=exists, saved_at=saved_at, pending=_login_state.get('page') is not None)


@app.route('/api/login/start', methods=['POST'])
def api_login_start():
    try:
        with _lock:
            run_in_worker(_login_start)
    except Exception as e:
        return jsonify(error=str(e)), 500
    return jsonify(ok=True)


@app.route('/api/login/confirm', methods=['POST'])
def api_login_confirm():
    try:
        with _lock:
            run_in_worker(_login_finish, True)
    except Exception as e:
        return jsonify(error=str(e)), 500
    return jsonify(ok=True)


@app.route('/api/login/cancel', methods=['POST'])
def api_login_cancel():
    try:
        with _lock:
            run_in_worker(_login_finish, False)
    except Exception as e:
        return jsonify(error=str(e)), 500
    return jsonify(ok=True)


@app.route('/')
def index():
    return INDEX_HTML.replace('__API_TOKEN__', API_TOKEN)


def parse_mode_params(mode, data):
    """요청 데이터에서 모드별 필수 파라미터를 꺼낸다. 문제가 있으면 에러 메시지를 반환."""
    if mode == 'add':
        tag = (data.get('tag') or '').strip()
        if not tag:
            return None, '추가할 태그를 입력하세요.'
        return {'tag': tag}, None
    if mode == 'edit':
        old_tag = (data.get('old_tag') or '').strip()
        new_tag = (data.get('new_tag') or '').strip()
        if not old_tag or not new_tag:
            return None, '기존 태그와 새 태그를 모두 입력하세요.'
        return {'old_tag': old_tag, 'new_tag': new_tag}, None
    return None, f'알 수 없는 모드입니다: {mode}'


@app.route('/api/preview', methods=['POST'])
def api_preview():
    data = request.get_json(force=True)
    mode = (data.get('mode') or 'add').strip()
    dcodes = parse_dcodes(data.get('dcodes') or '')
    params, err = parse_mode_params(mode, data)
    if not dcodes or err:
        return jsonify(error=(err or 'D코드를 입력하세요.')), 400

    results = []
    with _lock:
        for dcode in dcodes:
            try:
                _, field = run_in_worker(load_field, dcode)
            except LoginExpired as e:
                results.append({'dcode': dcode, 'status': 'login_expired', 'error': str(e)})
                break
            except RuntimeError as e:
                results.append({'dcode': dcode, 'status': 'error', 'error': str(e)})
                continue

            current = field['value'] or ''
            new_value, status = compute_value(mode, current, params)
            if status in ('already_present', 'not_found', 'no_change'):
                results.append({'dcode': dcode, 'status': status, 'current': current})
            else:
                results.append({'dcode': dcode, 'status': 'ok', 'current': current, 'new_value': new_value})

    return jsonify(mode=mode, results=results, **params)


@app.route('/api/apply', methods=['POST'])
def api_apply():
    data = request.get_json(force=True)
    mode = (data.get('mode') or 'add').strip()
    params, err = parse_mode_params(mode, data)
    items = data.get('items') or []  # [{dcode, expected_new}, ...] -- only 'ok' rows from preview
    if err or not items:
        return jsonify(error=(err or '잘못된 요청입니다. 먼저 확인(preview)을 실행하세요.')), 400

    results = []
    with _lock:
        for item in items:
            dcode = item.get('dcode')
            expected_new = item.get('expected_new')
            try:
                results.append(run_in_worker(_apply_one, dcode, mode, params, expected_new))
            except LoginExpired as e:
                results.append({'dcode': dcode, 'status': 'login_expired', 'error': str(e)})
                break
    return jsonify(mode=mode, results=results, **params)


def _apply_one(dcode, mode, params, expected_new):
    try:
        page, field = load_field(dcode)
    except LoginExpired:
        raise
    except RuntimeError as e:
        return {'dcode': dcode, 'status': 'error', 'error': str(e)}

    current = field['value'] or ''
    new_value, status = compute_value(mode, current, params)

    entry = {'timestamp': now_iso(), 'dcode': dcode, 'group_idx': None,
              'raw': params.get('old_tag'), 'canon': params.get('tag') or params.get('new_tag'),
              'action': 'add_tag' if mode == 'add' else 'edit_tag'}

    if status in ('already_present', 'not_found', 'no_change'):
        entry.update(status=f'skipped_{status}', old_value=current, verified=True)
        append_audit(entry)
        return {'dcode': dcode, 'status': status, 'current': current}

    if new_value != expected_new:
        entry.update(status='skipped_stale', old_value=current, new_value=new_value,
                      expected=expected_new, verified=False)
        append_audit(entry)
        return {'dcode': dcode, 'status': 'stale',
                'error': '확인한 이후 값이 바뀐 것 같습니다. 다시 확인(preview)해주세요.'}

    input_el = page.locator(f'xpath={XPATH_SEARCH_INPUT}')
    input_el.click()
    page.keyboard.press('ControlOrMeta+a')
    input_el.fill(new_value)

    readback = read_search_field(page)
    if readback.get('value') != new_value:
        entry.update(status='failed_typing_mismatch', old_value=current, new_value=new_value,
                      actual=readback.get('value'), verified=False)
        append_audit(entry)
        return {'dcode': dcode, 'status': 'error', 'error': '입력 확인에 실패했습니다. 저장하지 않았습니다.'}

    dialog_messages = []
    def on_dialog(dialog):
        dialog_messages.append(dialog.message)
        dialog.dismiss()
    page.on('dialog', on_dialog)

    find_save_button(page).click()
    page.wait_for_timeout(600)

    if dialog_messages:
        entry.update(status='aborted_dialog', old_value=current, new_value=new_value,
                      dialog_messages=dialog_messages, verified=False)
        append_audit(entry)
        return {'dcode': dcode, 'status': 'error', 'error': f'저장 중 확인창이 나타났습니다: {dialog_messages}'}

    url = BASE_URL.format(dcode)
    page.goto(url, wait_until='networkidle')
    verify_field = wait_for_stable_field(page)
    if verify_field.get('value') != new_value:
        entry.update(status='failed_verification', old_value=current, new_value=new_value,
                      actual_after_reload=verify_field.get('value'), verified=False)
        append_audit(entry)
        return {'dcode': dcode, 'status': 'error',
                'error': f'저장 후 재확인 실패. 기대값={new_value!r}, 실제값={verify_field.get("value")!r}'}

    entry.update(status='success', old_value=current, new_value=new_value, verified=True)
    append_audit(entry)
    return {'dcode': dcode, 'status': 'success', 'old_value': current, 'new_value': new_value}


INDEX_HTML = """
<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>CRM 검색어 태그 관리</title>
<style>
  :root {
    --add: #16a34a; --add-dark: #15803d; --edit: #d97706; --edit-dark: #b45309;
    --login: #2563eb; --login-dark: #1d4ed8;
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "Malgun Gothic", "Segoe UI", sans-serif;
    max-width: 900px; margin: 32px auto; padding: 0 16px;
    background: #f3f4f6; color: #111827;
  }
  h1 { font-size: 21px; margin-bottom: 2px; }
  .subtitle { color: #6b7280; font-size: 13px; margin-top: 0; }

  .banner {
    display: flex; align-items: flex-start; gap: 10px;
    background: #eff6ff; border: 1px solid #bfdbfe; color: #1e3a8a;
    border-radius: 10px; padding: 12px 14px; font-size: 13px; margin: 16px 0 20px;
  }
  .banner b { color: #1d4ed8; }

  .tabs { display: flex; gap: 4px; }
  .tab-btn {
    flex: 1; padding: 13px 16px; font-size: 14px; font-weight: 700; cursor: pointer;
    border: none; background: #e5e7eb; color: #4b5563;
    border-radius: 10px 10px 0 0; transition: background .12s, color .12s;
  }
  .tab-btn .desc { display: block; font-weight: 400; font-size: 11px; margin-top: 2px; opacity: .85; }
  .tab-btn.active-add { background: var(--add); color: #fff; }
  .tab-btn.active-edit { background: var(--edit); color: #fff; }
  .tab-btn.active-login { background: var(--login); color: #fff; }
  .tab-btn.need-login { box-shadow: 0 0 0 2px var(--login) inset; }

  .panel {
    background: #fff; border-radius: 0 0 12px 12px; padding: 24px;
    box-shadow: 0 1px 3px rgba(0,0,0,.08); border-top: 5px solid transparent;
  }
  .panel.mode-add { border-top-color: var(--add); }
  .panel.mode-edit { border-top-color: var(--edit); }
  .panel.mode-login { border-top-color: var(--login); }
  .panel.hidden { display: none; }

  .login-status { display: flex; align-items: center; gap: 10px; padding: 12px 14px; border-radius: 10px; font-size: 13px; margin-bottom: 4px; }
  .login-status.is-ok { background: #dcfce7; color: #166534; }
  .login-status.is-none { background: #fef3c7; color: #92400e; }
  .btn-login { margin-top: 16px; padding: 11px 20px; color: #fff; background: var(--login); }
  .btn-login:hover:not(:disabled) { background: var(--login-dark); }
  .btn-login-secondary { margin-top: 16px; margin-left: 8px; padding: 11px 20px; background: #fff; color: #374151; border: 1px solid #d1d5db; }
  .login-steps { margin-top: 18px; padding: 14px 16px; background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 10px; font-size: 13px; color: #374151; }
  .login-steps li { margin: 4px 0; }

  label { display: block; margin-top: 16px; font-weight: 700; font-size: 13px; color: #374151; }
  .hint { color: #9ca3af; font-size: 12px; margin: 3px 0 0; }
  input, textarea {
    width: 100%; padding: 9px 10px; font-size: 14px; box-sizing: border-box;
    margin-top: 6px; font-family: inherit; border: 1px solid #d1d5db; border-radius: 8px;
    background: #f9fafb;
  }
  input:focus, textarea:focus { outline: none; border-color: #9ca3af; background: #fff; }
  textarea { height: 64px; resize: vertical; }
  .two-col { display: flex; gap: 12px; }
  .two-col > div { flex: 1; }

  button { cursor: pointer; border: none; font-weight: 700; font-size: 14px; border-radius: 8px; }
  button:disabled { opacity: .55; cursor: default; }
  .btn-preview {
    margin-top: 18px; padding: 11px 20px; color: #fff;
  }
  .mode-add .btn-preview { background: var(--add); }
  .mode-add .btn-preview:hover:not(:disabled) { background: var(--add-dark); }
  .mode-edit .btn-preview { background: var(--edit); }
  .mode-edit .btn-preview:hover:not(:disabled) { background: var(--edit-dark); }

  .btn-save {
    display: none; margin-top: 14px; padding: 11px 20px;
    background: #fff; color: #991b1b; border: 2px solid #991b1b;
  }
  .btn-save:hover:not(:disabled) { background: #fef2f2; }

  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 13px; }
  thead th { position: sticky; top: 0; background: #f4f4f5; }
  th, td { border-bottom: 1px solid #e5e7eb; padding: 8px 10px; text-align: left; vertical-align: top; }
  tbody tr:hover { background: #fafafa; }
  .mono { font-family: "SFMono-Regular", Consolas, monospace; white-space: nowrap; }
  .old { color: #b91c1c; }
  .new { color: #15803d; font-weight: 700; }
  .err-msg { color: #991b1b; font-size: 12px; margin-top: 3px; }

  .pill { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 12px; font-weight: 700; white-space: nowrap; }
  .pill-ok { background: #dcfce7; color: #166534; }
  .pill-warn { background: #fef3c7; color: #92400e; }
  .pill-err { background: #fee2e2; color: #991b1b; }

  #addSummary, #editSummary { margin-top: 14px; font-weight: 700; font-size: 14px; }
  .msg-ok { color: #166534; }
  .msg-err { color: #991b1b; }
</style>
</head>
<body>
  <h1>CRM 검색어 태그 관리</h1>
  <p class="subtitle">여러 D코드를 한 번에 처리합니다. 확인(미리보기) 단계에서는 CRM이 절대 수정되지 않습니다.</p>

  <div class="banner">
    <span>⚠️</span>
    <div><b>전체 저장</b>은 실제 CRM 데이터를 수정하며 되돌리기 어렵습니다. 항상 먼저 <b>확인</b>으로 변경 내용을 검토하세요.</div>
  </div>

  <div class="tabs">
    <button type="button" id="tabBtnLogin" class="tab-btn" onclick="switchTab('login')">
      🔑 로그인
      <span class="desc">CRM 로그인 세션 만들기/재로그인</span>
    </button>
    <button type="button" id="tabBtnAdd" class="tab-btn" onclick="switchTab('add')">
      ➕ 태그 추가
      <span class="desc">검색어에 새 태그를 덧붙입니다</span>
    </button>
    <button type="button" id="tabBtnEdit" class="tab-btn" onclick="switchTab('edit')">
      ✏️ 태그 수정
      <span class="desc">이미 붙어 있는 태그를 다른 태그로 바꿉니다</span>
    </button>
  </div>

  <!-- 로그인 -->
  <div id="panelLogin" class="panel mode-login hidden">
    <div id="loginStatus" class="login-status is-none">확인 중...</div>

    <div class="login-steps">
      <b>사용 방법</b>
      <ol>
        <li>아래 <b>"로그인 창 열기"</b>를 누르면 브라우저 창이 새로 열립니다.</li>
        <li>그 창에서 CRM(crm.mathflat.com)에 <b>직접 로그인</b>합니다 (아이디/비밀번호는 본인이 입력).</li>
        <li>로그인이 끝나면 이 페이지로 돌아와 <b>"로그인 완료"</b>를 누릅니다. 그러면 창이 닫히고 세션이 저장됩니다.</li>
        <li>이후 "태그 추가"/"태그 수정" 탭을 바로 사용할 수 있습니다. 세션은 만료되기 전까지 계속 재사용됩니다.</li>
      </ol>
    </div>

    <button class="btn-login" id="loginStartBtn" onclick="loginTool.start()">로그인 창 열기</button>
    <button class="btn-login-secondary hidden" id="loginConfirmBtn" onclick="loginTool.confirm()">✅ 로그인 완료</button>
    <button class="btn-login-secondary hidden" id="loginCancelBtn" onclick="loginTool.cancel()">취소</button>
    <div id="loginSummary" style="margin-top:12px;font-weight:700;"></div>
  </div>

  <!-- 태그 추가 -->
  <div id="panelAdd" class="panel mode-add">
    <label>D코드 (띄어쓰기, 콤마, 줄바꿈으로 구분)</label>
    <textarea id="addDcodes" placeholder="D12345 D23456, D34567"></textarea>
    <p class="hint">여러 개를 한 번에 넣을 수 있습니다.</p>

    <label>추가할 태그</label>
    <input id="addTag" placeholder="예: [체험소진]">
    <p class="hint">이미 그 태그가 있는 D코드는 건너뜁니다.</p>

    <button class="btn-preview" id="addPreviewBtn" onclick="addTool.preview()">확인</button>
    <div class="table-wrap" id="addTableWrap"></div>
    <button class="btn-save" id="addSaveBtn" onclick="addTool.apply()">⚠️ 전체 저장 (CRM 실제 수정)</button>
    <div id="addSummary"></div>
  </div>

  <!-- 태그 수정 -->
  <div id="panelEdit" class="panel mode-edit hidden">
    <label>D코드 (띄어쓰기, 콤마, 줄바꿈으로 구분)</label>
    <textarea id="editDcodes" placeholder="D12345 D23456, D34567"></textarea>
    <p class="hint">이 D코드들의 검색어 중에서 아래 "기존 태그"를 찾습니다.</p>

    <div class="two-col">
      <div>
        <label>찾을 기존 태그 (정확히 일치)</label>
        <input id="editOldTag" placeholder="예: [체험소진]">
      </div>
      <div>
        <label>바꿀 새 태그</label>
        <input id="editNewTag" placeholder="예: [체험소진_완료]">
      </div>
    </div>
    <p class="hint">해당 D코드의 검색어에 "기존 태그"가 없으면 건너뜁니다(다른 태그는 그대로 둠).</p>

    <button class="btn-preview" id="editPreviewBtn" onclick="editTool.preview()">확인</button>
    <div class="table-wrap" id="editTableWrap"></div>
    <button class="btn-save" id="editSaveBtn" onclick="editTool.apply()">⚠️ 전체 저장 (CRM 실제 수정)</button>
    <div id="editSummary"></div>
  </div>

<script>
const API_TOKEN = "__API_TOKEN__";
function apiFetch(url, opts) {
  opts = opts || {};
  opts.headers = Object.assign({}, opts.headers, {'X-Api-Token': API_TOKEN});
  return fetch(url, opts);
}
function el(id) { return document.getElementById(id); }
function msgOk(text) { return '<span class="msg-ok">' + text + '</span>'; }
function msgErr(text) { return '<span class="msg-err">' + text + '</span>'; }

function switchTab(name) {
  el('tabBtnLogin').classList.toggle('active-login', name === 'login');
  el('tabBtnAdd').classList.toggle('active-add', name === 'add');
  el('tabBtnEdit').classList.toggle('active-edit', name === 'edit');
  el('panelLogin').classList.toggle('hidden', name !== 'login');
  el('panelAdd').classList.toggle('hidden', name !== 'add');
  el('panelEdit').classList.toggle('hidden', name !== 'edit');
}

const STATUS_LABEL = {
  ok: ['ok', '변경 예정'],
  success: ['ok', '저장 완료'],
  already_present: ['warn', '이미 있음(스킵)'],
  not_found: ['warn', '해당 태그 없음(스킵)'],
  no_change: ['warn', '기존=신규(스킵)'],
  login_expired: ['err', '로그인 만료'],
  stale: ['err', '값이 바뀜(스킵)'],
};

function statusPill(status) {
  const [cls, label] = STATUS_LABEL[status] || ['err', '오류'];
  return '<span class="pill pill-' + cls + '">' + label + '</span>';
}

function renderTable(results, wrapId, isApplyResult) {
  const oldHeader = isApplyResult ? '이전 값' : '현재 검색어';
  const newHeader = isApplyResult ? '새 값' : '변경 후';
  let rows = '<table><thead><tr><th>D코드</th><th>' + oldHeader + '</th><th>' + newHeader +
    '</th><th>상태</th></tr></thead><tbody>';
  for (const r of results) {
    const oldVal = (r.old_value !== undefined ? r.old_value : r.current) || '';
    rows += '<tr><td class="mono">' + r.dcode + '</td>' +
      '<td class="old">' + (oldVal || '(없음)') + '</td>' +
      '<td class="new">' + (r.new_value || '-') + '</td>' +
      '<td>' + statusPill(r.status) + (r.error ? '<div class="err-msg">' + r.error + '</div>' : '') + '</td></tr>';
  }
  rows += '</tbody></table>';
  el(wrapId).innerHTML = rows;
}

function makeTool(mode, ids) {
  let lastPreview = null;

  function setBusy(btnId, busy, busyText, idleText) {
    const btn = el(btnId);
    btn.disabled = busy;
    btn.textContent = busy ? busyText : idleText;
  }

  function buildPayload() {
    const dcodes = el(ids.dcodes).value.trim();
    if (mode === 'add') {
      const tag = el(ids.tag).value.trim();
      if (!dcodes || !tag) return {error: 'D코드와 태그를 모두 입력하세요.'};
      return {mode, dcodes, tag};
    }
    const old_tag = el(ids.oldTag).value.trim();
    const new_tag = el(ids.newTag).value.trim();
    if (!dcodes || !old_tag || !new_tag) return {error: 'D코드, 기존 태그, 새 태그를 모두 입력하세요.'};
    return {mode, dcodes, old_tag, new_tag};
  }

  async function preview() {
    el(ids.summary).innerHTML = '';
    el(ids.tableWrap).innerHTML = '';
    el(ids.saveBtn).style.display = 'none';
    lastPreview = null;

    const payload = buildPayload();
    if (payload.error) { el(ids.summary).innerHTML = msgErr(payload.error); return; }

    setBusy(ids.previewBtn, true, '확인 중...', '확인');
    let data;
    try {
      const res = await apiFetch('/api/preview', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
      });
      data = await res.json();
      if (!res.ok || data.error) { el(ids.summary).innerHTML = msgErr(data.error || '오류'); return; }
    } catch (e) {
      el(ids.summary).innerHTML = msgErr('요청 실패: ' + e); return;
    } finally {
      setBusy(ids.previewBtn, false, '확인 중...', '확인');
    }

    renderTable(data.results, ids.tableWrap, false);
    const applyItems = data.results.filter(r => r.status === 'ok').map(r => ({dcode: r.dcode, expected_new: r.new_value}));
    if (applyItems.length > 0) {
      lastPreview = Object.assign({}, payload, {items: applyItems});
      delete lastPreview.dcodes;
      el(ids.saveBtn).style.display = 'inline-block';
      el(ids.summary).innerHTML = msgOk(applyItems.length + '건 저장 가능 (총 ' + data.results.length + '건 중)');
    } else {
      el(ids.summary).innerHTML = msgErr('저장할 수 있는 건이 없습니다.');
    }
  }

  async function apply() {
    if (!lastPreview) return;
    const n = lastPreview.items.length;
    const desc = mode === 'add'
      ? "'" + lastPreview.tag + "' 태그 추가"
      : "'" + lastPreview.old_tag + "' → '" + lastPreview.new_tag + "'로 수정";
    if (!confirm(n + '건에 대해 ' + desc + '을(를) 실행합니다.\\n실제 CRM 데이터가 바뀌며 되돌리기 어렵습니다. 계속할까요?')) return;

    setBusy(ids.saveBtn, true, '저장 중...', '⚠️ 전체 저장 (CRM 실제 수정)');
    let data;
    try {
      const res = await apiFetch('/api/apply', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(lastPreview)
      });
      data = await res.json();
      if (!res.ok || data.error) { el(ids.summary).innerHTML = msgErr(data.error || '오류'); return; }
    } catch (e) {
      el(ids.summary).innerHTML = msgErr('요청 실패: ' + e); return;
    } finally {
      setBusy(ids.saveBtn, false, '저장 중...', '⚠️ 전체 저장 (CRM 실제 수정)');
    }

    renderTable(data.results, ids.tableWrap, true);
    const successCount = data.results.filter(r => r.status === 'success').length;
    el(ids.summary).innerHTML = msgOk(successCount + '건 저장 완료 (총 ' + data.results.length + '건 시도)');
    el(ids.saveBtn).style.display = 'none';
    lastPreview = null;
  }

  return {preview, apply};
}

const addTool = makeTool('add', {
  dcodes: 'addDcodes', tag: 'addTag', previewBtn: 'addPreviewBtn',
  saveBtn: 'addSaveBtn', tableWrap: 'addTableWrap', summary: 'addSummary'
});
const editTool = makeTool('edit', {
  dcodes: 'editDcodes', oldTag: 'editOldTag', newTag: 'editNewTag', previewBtn: 'editPreviewBtn',
  saveBtn: 'editSaveBtn', tableWrap: 'editTableWrap', summary: 'editSummary'
});

const loginTool = (function () {
  function showPending(pending) {
    el('loginStartBtn').classList.toggle('hidden', pending);
    el('loginConfirmBtn').classList.toggle('hidden', !pending);
    el('loginCancelBtn').classList.toggle('hidden', !pending);
  }

  async function refreshStatus() {
    let data;
    try {
      const res = await apiFetch('/api/login/status');
      data = await res.json();
    } catch (e) {
      el('loginStatus').className = 'login-status is-none';
      el('loginStatus').textContent = '상태 확인 실패: ' + e;
      return null;
    }
    const box = el('loginStatus');
    if (data.exists) {
      box.className = 'login-status is-ok';
      box.innerHTML = '✅ 로그인 세션 저장됨 (' + (data.saved_at || '-') + '). 세션이 만료되면 다시 로그인하세요.';
    } else {
      box.className = 'login-status is-none';
      box.innerHTML = '⚠️ 아직 로그인 세션이 없습니다. 아래에서 먼저 로그인하세요.';
    }
    showPending(!!data.pending);
    return data;
  }

  async function start() {
    el('loginSummary').innerHTML = '';
    el('loginStartBtn').disabled = true;
    el('loginStartBtn').textContent = '창 여는 중...';
    try {
      const res = await apiFetch('/api/login/start', {method: 'POST'});
      const data = await res.json();
      if (!res.ok || data.error) { el('loginSummary').innerHTML = msgErr(data.error || '오류'); return; }
      showPending(true);
      el('loginSummary').innerHTML = msgOk('브라우저 창이 열렸습니다. 그 창에서 로그인한 뒤 "로그인 완료"를 누르세요.');
    } catch (e) {
      el('loginSummary').innerHTML = msgErr('요청 실패: ' + e);
    } finally {
      el('loginStartBtn').disabled = false;
      el('loginStartBtn').textContent = '로그인 창 열기';
    }
  }

  async function confirm_() {
    el('loginConfirmBtn').disabled = true;
    el('loginConfirmBtn').textContent = '저장 중...';
    try {
      const res = await apiFetch('/api/login/confirm', {method: 'POST'});
      const data = await res.json();
      if (!res.ok || data.error) { el('loginSummary').innerHTML = msgErr(data.error || '오류'); return; }
      el('loginSummary').innerHTML = msgOk('로그인 세션을 저장했습니다. 이제 "태그 추가"/"태그 수정" 탭을 사용할 수 있습니다.');
      await refreshStatus();
    } catch (e) {
      el('loginSummary').innerHTML = msgErr('요청 실패: ' + e);
    } finally {
      el('loginConfirmBtn').disabled = false;
      el('loginConfirmBtn').textContent = '✅ 로그인 완료';
    }
  }

  async function cancel() {
    el('loginCancelBtn').disabled = true;
    try {
      const res = await apiFetch('/api/login/cancel', {method: 'POST'});
      const data = await res.json();
      if (!res.ok || data.error) { el('loginSummary').innerHTML = msgErr(data.error || '오류'); return; }
      el('loginSummary').innerHTML = '로그인 창을 닫았습니다 (저장 안 함).';
      await refreshStatus();
    } catch (e) {
      el('loginSummary').innerHTML = msgErr('요청 실패: ' + e);
    } finally {
      el('loginCancelBtn').disabled = false;
    }
  }

  return {start, confirm: confirm_, cancel, refreshStatus};
})();

(async function init() {
  const status = await loginTool.refreshStatus();
  switchTab(status && status.exists ? 'add' : 'login');
})();
</script>
</body>
</html>
"""


if __name__ == '__main__':
    print('http://127.0.0.1:5050 에서 열어주세요.')
    app.run(host='127.0.0.1', port=5050, debug=False)
