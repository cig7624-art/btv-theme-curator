"""Conversation and draft UI for the shared AI creative director engine."""
from __future__ import annotations

from copy import deepcopy
import html
from pathlib import Path
from collections.abc import Mapping

import pandas as pd
import streamlit as st

from ai_director import _undo_version, new_director_session, restore_director_version, run_director_turn
from ai_director_storage import (
    DirectorDuplicateError, DirectorStorageError, adopt_director_draft,
    director_adoption_key,
)
from llm_theme_generator import ThemeGenerationError, github_writeback_configured
from theme_editorial import _candidate, _poster_html

SESSION_KEY = "ai_director_session"
_SCOPE_LABELS = {
    "auto": "요청에 맞게", "title": "테마명만", "copy": "서브카피만",
    "contents": "추천 작품만", "style": "장르·분위기", "all": "기획 전체",
}
_QUICK_REQUESTS = (
    ("뭐 만들어볼까?", "뭐 만들어볼까?"),
    ("분위기부터 골라볼까?", "늦은 밤에 어울리는 색다른 큐레이션을 기획해 보자. 어떤 방향이 좋을까?"),
    ("한국 영화·드라마로 3개", "한국 영화와 드라마를 섞어서 감각적인 큐레이션 테마 3개 만들어줘."),
)


def _text(value):
    return str(value or "").strip()


def _escape(value):
    return html.escape(_text(value), quote=True)


def activate_director_tab():
    """A widget callback runs before tabs are registered on the next rerun."""
    st.session_state["page"] = "home"
    st.session_state["home_tab_default"] = "AI 디렉터"
    st.session_state["home_sections"] = "AI 디렉터"


def _show_saved_themes():
    st.session_state["page"] = "home"
    st.session_state["home_tab_default"] = "추천 테마"
    st.session_state["home_sections"] = "추천 테마"


def director_draft_card_html(index, draft):
    """Reuse saved-theme poster rendering while keeping free-form briefs honest."""
    raw = draft.get("contents") or []
    candidates = [_candidate(item) for item in raw if isinstance(item, Mapping)]
    candidates = [item for item in candidates if item]
    posters = []
    for position, item in enumerate(candidates[:10]):
        reason = ('<p class="director-content-reason">' + _escape(item['reason']) + '</p>') if item['reason'] else ''
        posters.append('<div class="director-content">' + _poster_html(item, position) + reason + '</div>')
    content = ('<div class="director-content-rail" tabindex="0" aria-label="' + _escape(draft.get('theme_name'))
               + ' 추천 작품">' + ''.join(posters) + '</div>') if posters else '<p class="director-missing">아직 연결된 작품이 없어요. 조건을 넓히거나 작품을 다시 요청해 주세요.</p>'
    return ('<article class="director-draft-card" data-draft-id="' + _escape(draft.get('draft_id')) + '">'
            '<header class="director-draft-top"><span>DRAFT ' + f'{index:02d}' + '</span><span>'
            + _escape(' · '.join(_text(draft.get(key)) for key in ('genre', 'mood') if _text(draft.get(key)))) + '</span></header>'
            '<h3>' + _escape(draft.get('theme_name')) + '</h3>'
            '<p class="director-draft-copy">' + _escape(draft.get('copy')) + '</p>'
            '<div class="director-rationale"><span>기획 의도</span><p>' + _escape(draft.get('rationale')) + '</p></div>'
            '<div class="director-content-heading"><span>추천 콘텐츠</span><span>' + str(len(candidates)) + '편</span></div>'
            + content + '</article>')


def _queue_request(request, *, target=None, scope="auto"):
    st.session_state["director_pending_request"] = {"request": request, "target": target, "scope": scope}
    st.session_state["home_tab_default"] = "AI 디렉터"


def _previous_draft_version(session, draft):
    try:
        return _undo_version(session, target_draft_id=draft.get('draft_id'))
    except ThemeGenerationError:
        return None


def _render_saved_theme_matches(session, find_saved_themes, *, configured, busy):
    """Use the library's existing local ranking on the current chat intent."""
    intent = session.get('library_search_intent')
    if not isinstance(intent, Mapping) or find_saved_themes is None:
        return
    try:
        matches = find_saved_themes(intent)
        if isinstance(matches, pd.DataFrame):
            matches = matches.fillna('').to_dict('records')
        matches = [item for item in matches if isinstance(item, Mapping) and _text(item.get('theme_name'))][:10]
    except Exception:
        st.caption('라이브러리 자료를 불러오지 못했어요. 대화와 초안은 계속 이어갈 수 있습니다.')
        return
    session['library_search_results'] = [
        {'number': index + 1, **{key: _text(item.get(key)) for key in ('theme_id', 'theme_name', 'copy', 'genre')}}
        for index, item in enumerate(matches)
    ]
    with st.container(key='director_library_matches'):
        st.markdown('<div class="director-library-heading"><h3>라이브러리에서 찾은 테마</h3>'
                    '<span>저장한 기획에서 이어가기</span></div>', unsafe_allow_html=True)
        if not matches:
            st.caption('가까운 저장 테마가 없어요. 이 방향으로 새로운 기획을 함께 만들 수 있습니다.')
            return
        for index, item in enumerate(matches):
            name = _text(item.get('theme_name'))
            copy = _text(item.get('copy'))
            genre = _text(item.get('genre'))
            with st.container(key='director_library_match_' + str(index)):
                text_column, action_column = st.columns([3, 1], gap='small', vertical_alignment='center')
                with text_column:
                    st.markdown('<article class="director-library-card"><span>' + _escape(genre) + '</span>'
                                '<h4>' + str(index + 1) + '. ' + _escape(name) + '</h4><p>' + _escape(copy) + '</p></article>', unsafe_allow_html=True)
                with action_column:
                    if st.button('이 테마로 새롭게 기획', key='director_library_rework_' + str(index),
                                 width='stretch', disabled=not configured or busy):
                        _queue_request('라이브러리의 다음 테마에서 출발해 새로운 큐레이션 테마 1개를 기획해줘. '
                                       '기존 이름은 그대로 쓰지 말고, 앞선 대화의 취향과 조건을 반영해 제목·카피·기획 의도와 '
                                       '실제 콘텐츠 DB로 확인되는 추천 작품 5~10편을 만들어줘. '
                                       '참고 테마명: ' + name[:100] + '\n참고 카피: ' + copy[:300] + '\n참고 장르: ' + genre[:100])
                        st.rerun()


def _run_request(pending, *, session, themes, contents, issue_records, api_key, model, existing_candidates):
    st.session_state["director_busy"] = True
    try:
        with st.spinner("이야기를 읽고 함께 기획하고 있어요…"):
            updated = run_director_turn(
                session, pending['request'], themes=themes, contents=contents,
                issue_records=issue_records, api_key=api_key, model=model,
                target_draft_id=pending.get('target'), edit_scope=pending.get('scope', 'auto'),
                existing_candidates=existing_candidates,
            )
        st.session_state[SESSION_KEY] = updated
        st.session_state.pop('director_error', None)
        st.session_state.pop('director_failed_request', None)
    except ThemeGenerationError as error:
        st.session_state['director_error'] = str(error)
        st.session_state['director_failed_request'] = deepcopy(pending)
    except Exception:
        st.session_state['director_error'] = "요청을 마무리하지 못했어요. 기존 초안은 그대로 보관했습니다. 잠시 후 다시 시도해 주세요."
        st.session_state['director_failed_request'] = deepcopy(pending)
    finally:
        st.session_state['director_busy'] = False


def render_ai_director_page(*, themes, contents, issue_records, api_key, model,
                            github_config, theme_path, history_path, content_path,
                            existing_candidates=None, find_saved_themes=None):
    """Draft-only chat. The adoption button is the sole persistence action."""
    styles = Path(__file__).with_name('ai_director_styles.css').read_text(encoding='utf-8')
    st.markdown('<style>' + styles + '</style>', unsafe_allow_html=True)
    if SESSION_KEY not in st.session_state:
        st.session_state[SESSION_KEY] = new_director_session()
    session = st.session_state[SESSION_KEY]
    drafts = session.get('drafts', [])
    busy = bool(st.session_state.get('director_busy'))
    configured = bool(api_key)
    durable_ready = github_writeback_configured(github_config)
    adopted = st.session_state.setdefault('director_adopted', {})

    with st.container(key='ai_director_section'):
        st.markdown('<header class="director-heading"><span>AI CREATIVE DIRECTOR</span><h2>AI 디렉터</h2>'
                    '<p>떠오른 한마디에서 시작해, 함께 만드는 큐레이션.</p>'
                    '<div class="director-workflow">아이디어 나누기 <span>→</span> 함께 다듬기 <span>→</span> 최종 채택</div></header>', unsafe_allow_html=True)
        if not configured:
            st.info('AI 디렉터를 사용하려면 OpenAI 연결 설정이 필요합니다. 설정 후 자유롭게 테마를 요청할 수 있어요.')
        with st.container(key='director_quick_requests'):
            columns = st.columns(3, gap='small')
            for index, (column, (label, request)) in enumerate(zip(columns, _QUICK_REQUESTS)):
                with column:
                    if st.button(label, key='director_quick_' + str(index),
                                 width='stretch', disabled=not configured or busy,
                                 help='이 한마디로 AI 디렉터와 대화를 시작합니다.'):
                        _queue_request(request)
                        st.rerun()

        if drafts:
            with st.container(key='director_edit_context'):
                target_column, scope_column = st.columns([2, 1], gap='small')
                targets = {'': '새 기획 / 전체 초안', **{draft['draft_id']: f"{index}. {draft['theme_name']}" for index, draft in enumerate(drafts, 1)}}
                if st.session_state.get('director_target') not in targets:
                    st.session_state['director_target'] = ''
                with target_column:
                    selected_target = st.selectbox('수정할 테마', list(targets), format_func=targets.get, key='director_target')
                with scope_column:
                    selected_scope = st.selectbox('수정 범위', list(_SCOPE_LABELS), format_func=_SCOPE_LABELS.get, key='director_scope')
            versions = session.get('versions', [])
            if len(versions) > 1:
                with st.expander('이전 버전으로 되돌리기', expanded=False):
                    labels = {version['version_id']: f"{index}. {version.get('label') or '기획 수정'}" for index, version in enumerate(versions, 1)}
                    current = session.get('current_version_id')
                    if st.session_state.get('director_restore_version') not in labels:
                        st.session_state['director_restore_version'] = current if current in labels else list(labels)[-1]
                    version_id = st.selectbox('복구할 버전', list(labels), format_func=labels.get, key='director_restore_version')
                    if st.button('이 버전으로 되돌리기', key='director_restore', disabled=busy or version_id == current):
                        st.session_state[SESSION_KEY] = restore_director_version(session, version_id)
                        st.rerun()
        else:
            selected_target, selected_scope = '', 'auto'

        with st.container(key='director_conversation'):
            for message in session.get('messages', []):
                role = 'user' if message.get('role') == 'user' else 'assistant'
                with st.chat_message(role):
                    st.markdown(_text(message.get('content')))
            if not session.get('messages'):
                st.markdown('<div class="director-conversation-empty"><strong>뭐 만들어볼까요?</strong>'
                            '<p>취향이나 분위기 하나만 꺼내 주세요. 아직 생각이 없다면 “뭐 만들어볼까?”도 좋아요.<br>'
                            '함께 방향을 고르고, 제목과 작품을 대화로 다듬어 봐요.</p></div>', unsafe_allow_html=True)

        with st.container(key='director_composer'):
            prompt = st.chat_input('만들고 싶은 테마나 고치고 싶은 부분을 알려주세요.', key='director_chat_input',
                                   disabled=not configured or busy, max_chars=4000)
        pending = st.session_state.pop('director_pending_request', None)
        if prompt:
            pending = {'request': prompt, 'target': selected_target or None, 'scope': selected_scope}
        if pending and configured and not busy:
            _run_request(pending, session=session, themes=themes, contents=contents,
                         issue_records=issue_records, api_key=api_key, model=model,
                         existing_candidates=existing_candidates)
            st.rerun()

        if st.session_state.get('director_error'):
            st.error(st.session_state['director_error'])
            failed = st.session_state.get('director_failed_request')
            if failed and st.button('이 요청 다시 시도', key='director_retry_request', disabled=not configured or busy):
                st.session_state['director_pending_request'] = deepcopy(failed)
                st.rerun()

        if not drafts:
            _render_saved_theme_matches(session, find_saved_themes, configured=configured, busy=busy)
            st.caption('초안은 이 대화에만 보관됩니다. 최종 채택한 테마만 추천 테마와 라이브러리에 저장돼요.')
            return

        st.markdown('<div class="director-results-heading"><h3>함께 다듬는 초안</h3><span>' + str(len(drafts)) + '개의 기획</span></div>', unsafe_allow_html=True)
        for index, draft in enumerate(drafts, 1):
            draft_id = draft['draft_id']
            fingerprint = director_adoption_key(draft)
            saved = adopted.get(fingerprint)
            with st.container(key='director_draft_' + draft_id):
                st.markdown(director_draft_card_html(index, draft), unsafe_allow_html=True)
                if draft.get('duplicate_matches'):
                    with st.expander('기존 테마와 겹치는 부분', expanded=False):
                        for match in draft['duplicate_matches'][:3]:
                            if isinstance(match, Mapping):
                                st.caption(_text(match.get('theme_name') or match.get('name') or match.get('theme_id')))
                            else:
                                st.caption(_text(match))
                rows = draft.get('contents') or []
                count = sum(isinstance(item, Mapping) and item.get('catalog_status') in {'confirmed_local', 'verified_external'} for item in rows)
                if count < 5:
                    st.caption(f'확인된 작품이 부족해요({count}/5편). 조건을 넓히거나 작품을 다시 요청해 주세요.')
                edit_column, restore_column, adopt_column = st.columns([1, 1, 1], gap='small')
                with edit_column:
                    with st.popover('수정하기', width='stretch'):
                        with st.form('director_edit_form_' + draft_id):
                            scope = st.selectbox('바꿀 부분', list(_SCOPE_LABELS), format_func=_SCOPE_LABELS.get,
                                                 key='director_card_scope_' + draft_id)
                            request = st.text_area('이 테마에 요청할 내용', placeholder='제목만 더 감각적으로 바꿔줘. / 특정 작품은 제외해줘.',
                                                   key='director_card_request_' + draft_id, max_chars=4000)
                            submitted = st.form_submit_button('수정 요청', disabled=not configured or busy)
                        if submitted and request.strip():
                            _queue_request(request.strip(), target=draft_id, scope=scope)
                            st.rerun()
                with restore_column:
                    previous = _previous_draft_version(session, draft)
                    if st.button('이전 버전', key='director_undo_' + draft_id, width='stretch', disabled=busy or not previous):
                        st.session_state[SESSION_KEY] = restore_director_version(session, previous, target_draft_id=draft_id)
                        st.rerun()
                with adopt_column:
                    if st.button('채택 완료' if saved else '최종 채택', key='director_adopt_' + draft_id, type='primary',
                                 width='stretch', disabled=busy or bool(saved) or not durable_ready or not 5 <= count <= 10 or len(rows) > 10):
                        try:
                            with st.spinner('채택한 테마와 추천 작품을 저장하고 있어요…'):
                                result = adopt_director_draft(draft, themes=themes, theme_path=Path(theme_path),
                                    history_path=Path(history_path), content_path=Path(content_path),
                                    github_config=github_config, model=model, catalog=contents)
                            if result.get('status') in {'success', 'already_adopted'}:
                                st.session_state['director_adopted'][fingerprint] = result
                                st.session_state['director_new_saved_run_id'] = result.get('run_id')
                                st.session_state['director_adoption_notice'] = f"‘{draft['theme_name']}’을 추천 테마와 라이브러리에 저장했어요."
                                st.session_state['director_refresh_required'] = result.get('local_status') == 'refresh_required'
                                st.rerun()
                            else:
                                st.error('영구 저장을 완료하지 못했어요. 초안은 그대로 보관했습니다.')
                        except DirectorDuplicateError as error:
                            st.warning(str(error))
                        except DirectorStorageError as error:
                            st.error(str(error))
                        except Exception:
                            st.error('저장을 완료하지 못했어요. 초안은 그대로 보관했습니다. 잠시 후 다시 시도해 주세요.')
                if saved:
                    st.caption('채택한 버전입니다. 이후 수정은 저장된 테마에 자동 반영되지 않아요.')
                elif not durable_ready:
                    st.caption('최종 채택하려면 테마 저장 연결 설정이 필요합니다. 초안 기획과 수정은 계속할 수 있어요.')

        if st.session_state.get('director_adoption_notice'):
            st.success(st.session_state['director_adoption_notice'])
            if st.session_state.get('director_refresh_required'):
                st.warning('원격 저장은 완료됐어요. 동시에 변경된 자료를 보호하기 위해 현재 파일은 그대로 두었습니다. 저장 자료를 다시 불러온 뒤 확인해 주세요.')
            st.button('추천 테마에서 보기', key='director_open_saved', on_click=_show_saved_themes)
        if session.get('library_search_intent') and find_saved_themes is not None:
            with st.expander('라이브러리에서 찾은 테마', expanded=False):
                _render_saved_theme_matches(session, find_saved_themes, configured=configured, busy=busy)
        st.caption('작품 정보와 포스터를 확인해 연결합니다. B tv의 판권·서비스 가능 여부를 확정하는 목록은 아닙니다.')
