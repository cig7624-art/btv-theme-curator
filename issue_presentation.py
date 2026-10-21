"""Consumer-facing explanations built from each issue's actual source measurements."""
import math
import re

import pandas as pd

from issue_quality import BOXOFFICE, NEWS, OTT, YOUTUBE, classify_source, read_issue_metrics
from issue_rules import (
    DAILY_COMMENT_DELTA_THRESHOLD, DAILY_VIEW_DELTA_THRESHOLD,
    GROWTH_MIN_DELTA, GROWTH_RATE_THRESHOLD, NEW_VIDEO_HIGH_VIEW_THRESHOLD,
    NEW_VIDEO_VIEWS_PER_DAY_THRESHOLD, YOUTUBE_PRIORITY_VIEW_DELTA,
)


def _number(value):
    try:
        number = float(str(value).replace(',', ''))
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _date(value):
    if value is None or str(value).strip() == '':
        return None
    try:
        result = pd.Timestamp(value)
        if pd.isna(result):
            return None
        return result.tz_convert('Asia/Seoul') if result.tzinfo else result.tz_localize('Asia/Seoul')
    except (ValueError, TypeError):
        return None


def _short_number(value):
    number = _number(value)
    if number is None:
        return ''
    return f'{number / 10000:,.1f}'.removesuffix('.0') + '만' if number >= 10000 else f'{int(number):,}'


def _full_number(value, suffix='회'):
    return f'{int(value):,}{suffix}'


def _period(start, end, label):
    dates = [value.strftime('%m.%d') for value in [start, end] if value is not None]
    return ('~'.join(dict.fromkeys(dates)) + ' ' + label) if dates else '자료 날짜 확인 필요'


def _detail_date(value):
    parsed = _date(value)
    if parsed is None:
        return '날짜 없음'
    return parsed.strftime('%Y-%m-%d %H:%M (한국 시간)') if isinstance(value, str) and ':' in value else parsed.strftime('%Y-%m-%d')


def present_issue(row):
    """Return simple card copy plus traceable detail; never infer whole-work views."""
    metrics = read_issue_metrics(row)
    group = row.get('source_group') or classify_source(row)
    reference = _date(row.get('_quality_reference') or row.get('reference_date') or row.get('published_at') or row.get('date'))
    details = []
    result = dict(source_label='콘텐츠 소식', headline='작품 관련 소식', reason='작품과 관련된 새 소식이 있어 선정했어요.', period_label=_period(reference, None, '기록'), details=details,
                  verification_note='수집한 날짜의 기록입니다. 원문의 현재 수치와 내용은 달라질 수 있습니다.')
    if group == YOUTUBE:
        result['source_label'] = 'YouTube'
        result['verification_note'] = '관련 영상 한 편의 반응이에요. 수집한 날짜의 기록으로, 현재 영상의 수치와는 차이가 있을 수 있어요.'
        views, change = _number(metrics.get('views_total')), _number(metrics.get('views_change'))
        start, end = _date(metrics.get('observation_start')), _date(metrics.get('observation_end'))
        valid_comparison = start is not None and end is not None and end > start and end <= pd.Timestamp.now(tz='Asia/Seoul') + pd.Timedelta(minutes=1)
        if 'comparison_valid' in row:
            valid_comparison = bool(row['comparison_valid'])
        days = (end - start).total_seconds() / 86400 if valid_comparison else None
        daily_change = change / days if change is not None and days else None
        comments_change = _number(metrics.get('comments_change'))
        daily_comments = comments_change / days if comments_change is not None and days else None
        comment_candidate = daily_comments is not None and daily_comments >= DAILY_COMMENT_DELTA_THRESHOLD
        if comment_candidate and (daily_change is None or daily_change < YOUTUBE_PRIORITY_VIEW_DELTA):
            result['headline'] = f'관련 영상 댓글 {_short_number(comments_change)} 개 증가'
            result['reason'] = f'하루 평균 댓글이 {_short_number(DAILY_COMMENT_DELTA_THRESHOLD)} 개 이상 늘어난 영상이 있어 선정했어요.'
            result['period_label'] = _period(start, end, '댓글 수 비교')
            details.extend([('댓글 수 비교 기간', _detail_date(metrics['observation_start']) + '~' + _detail_date(metrics['observation_end'])),
                            ('늘어난 댓글 수', _full_number(comments_change, '개')), ('하루 평균 댓글 증가', f'약 {_full_number(daily_comments, "개")}'),
                            ('이 카드의 선정 기준', f'관련 영상의 하루 평균 댓글 증가 {_full_number(DAILY_COMMENT_DELTA_THRESHOLD, "개")} 이상')])
        elif change is not None and change > 0 and valid_comparison:
            result['headline'] = f'관련 영상 조회수 {_short_number(change)} 회 증가'
            if daily_change >= DAILY_VIEW_DELTA_THRESHOLD:
                result['reason'] = f'하루 평균 조회수가 {_short_number(DAILY_VIEW_DELTA_THRESHOLD)} 회 이상 늘어난 영상이 있어 선정했어요.'
                rule = f'관련 영상의 하루 평균 조회수 증가 {_full_number(DAILY_VIEW_DELTA_THRESHOLD)} 이상'
            elif comment_candidate:
                result['reason'] = f'하루 평균 댓글이 {_short_number(DAILY_COMMENT_DELTA_THRESHOLD)} 개 이상 늘어난 영상이 있어 선정했어요.'
                rule = f'관련 영상의 하루 평균 댓글 증가 {_full_number(DAILY_COMMENT_DELTA_THRESHOLD, "개")} 이상'
                details.append(('늘어난 댓글 수', _full_number(comments_change, '개')))
            elif (_number(metrics.get('growth_rate')) or 0) >= GROWTH_RATE_THRESHOLD and daily_change >= GROWTH_MIN_DELTA:
                percent = f'{GROWTH_RATE_THRESHOLD * 100:g}%'
                result['reason'] = f'조회수가 이전보다 {percent} 이상 늘어난 영상이 있어 선정했어요.'
                rule = f'이전 조회수 대비 {percent} 이상 증가하고 하루 평균 조회수도 {_full_number(GROWTH_MIN_DELTA)} 이상 증가'
            elif daily_change >= YOUTUBE_PRIORITY_VIEW_DELTA:
                result['reason'] = f'하루 평균 조회수가 {_short_number(YOUTUBE_PRIORITY_VIEW_DELTA)} 회 이상 늘어난 영상이 있어 선정했어요.'
                rule = f'관련 영상의 하루 평균 조회수 증가 {_full_number(YOUTUBE_PRIORITY_VIEW_DELTA)} 이상'
            else:
                result['reason'] = '최근 공개된 관련 영상의 반응을 참고할 수 있어 소개해요.'
                rule = '최근 관련 영상의 반응을 참고 자료로 소개'
            result['period_label'] = _period(start, end, '조회수 비교')
            details.extend([('조회수 비교 기간', _detail_date(metrics['observation_start']) + '~' + _detail_date(metrics['observation_end'])),
                            ('관련 영상 조회수 증가', _full_number(change)), ('하루 평균 조회수 증가', f'약 {_full_number(daily_change)}'), ('이 카드의 선정 기준', rule)])
            if metrics.get('observation_precision') == 'date' or ':' not in str(metrics['observation_start']):
                details.append(('평균 계산', '두 집계 날짜 사이의 일수로 나눴어요. 과거 기록에는 정확한 집계 시각이 없어요.'))
        else:
            result['headline'] = f'관련 영상 누적 조회수 {_short_number(views)} 회' if views is not None else '관련 영상 공개'
            average = _number(metrics.get('views_per_day_since_upload'))
            if views is not None and views >= NEW_VIDEO_HIGH_VIEW_THRESHOLD:
                result['reason'] = f'누적 조회수 {_short_number(NEW_VIDEO_HIGH_VIEW_THRESHOLD)} 회 이상인 최근 영상이 있어 선정했어요.'
            elif average is not None and average >= NEW_VIDEO_VIEWS_PER_DAY_THRESHOLD:
                result['reason'] = f'공개 후 하루 평균 조회수 {_short_number(NEW_VIDEO_VIEWS_PER_DAY_THRESHOLD)} 회 이상인 영상이 있어 선정했어요.'
            else:
                result['reason'] = '최근 공개된 관련 영상의 반응을 참고할 수 있어 소개해요.'
            result['period_label'] = _period(end or reference, None, '조회수 집계')
            details.append(('이 카드의 선정 기준', result['reason']))
            if change == 0 and valid_comparison:
                details.append(('조회수 비교', '두 집계 사이에 조회수 증가가 없었어요.'))
            elif change is not None and change < 0 and valid_comparison:
                details.append(('조회수 비교', '조회수 기록이 줄어 증가 여부를 판단하지 않았어요.'))
            else:
                details.append(('조회수 비교', '이전 조회수 기록이 없어 얼마나 늘었는지는 아직 알 수 없어요.'))
        if views is not None:
            details.append(('관련 영상 누적 조회수', _full_number(views)))
        published = row.get('published_at')
        if published:
            details.append(('영상 공개일', _detail_date(published)))
    elif group == BOXOFFICE:
        result['source_label'] = '극장'
        rank, audience = _number(metrics.get('rank')), _number(metrics.get('daily_audience'))
        result['headline'] = f'극장 관객 {int(rank)}위' if rank is not None else '극장 관객 순위'
        result['reason'] = '일별 박스오피스 상위 10위에 오른 작품이에요.'
        result['period_label'] = _period(reference, None, f'관객 {_full_number(audience, "명")}' if audience is not None else '일별 집계')
        if rank is not None:
            details.append(('집계일 순위', f'{int(rank)}위'))
        if audience is not None:
            details.append(('하루 관객 수', _full_number(audience, '명')))
        details.extend([('집계일', _detail_date(row.get('reference_date') or row.get('_quality_reference'))), ('선정 기준', '영화진흥위원회 일별 박스오피스 상위 10위')])
    elif group == OTT and ('netflix' in str(row.get('source', '')).lower() or row.get('source_type') == 'ott_ranking'):
        result['source_label'] = 'Netflix'
        rank = _number(metrics.get('rank'))
        category = str(metrics.get('category', '')).lower()
        category_name = '영화' if 'film' in category or 'movie' in category else 'TV 프로그램' if 'tv' in category else '작품'
        result['headline'] = f'{category_name} 부문 {int(rank)}위' if rank is not None else f'{category_name} 부문 Top 10'
        result['reason'] = f'넷플릭스 한국 {category_name} 주간 Top 10에 오른 작품이에요.'
        end = reference
        start = end - pd.Timedelta(days=6) if end is not None else None
        result['period_label'] = _period(start, end, '주간 집계')
        details.extend([('집계 기간', f'{_detail_date(start)}~{_detail_date(end)}'), ('집계 지역·부문', f'한국 · {category_name}'), ('선정 기준', '해당 부문의 최신 주간 Top 10')])
    else:
        result['source_label'] = '공식 발표' if group == OTT and row.get('source_type') == 'official_announcement' else '뉴스'
        text = ' '.join(str(row.get(field, '')) for field in ['issue_title', 'description'])
        event = next((label for pattern, label in [(r'캐스팅|출연\s*확정', '캐스팅'), (r'제작\s*확정|영상화|리메이크', '제작'), (r'개봉', '개봉'), (r'공개|첫방|방영', '공개'), (r'수상|영화제', '수상·영화제'), (r'종영', '종영'), (r'인터뷰', '인터뷰')] if re.search(pattern, text)), '작품 관련')
        result['headline'] = f'{event} 소식'
        result['reason'] = f'최근 {event} 소식이 나온 작품이라 선정했어요.'
        published = _date(row.get('published_at'))
        if published is None:
            result['reason'] = f'저장된 자료에 {event} 소식이 있어 선정했어요.'
        result['period_label'] = _period(published or reference, None, '보도' if published is not None else '기록')
        if row.get('publisher'):
            details.append(('발행처', str(row['publisher'])))
        details.append(('기사 발행일' if published is not None else '자료 기록일', _detail_date(published or reference)))
        details.append(('선정 기준', '작품명이 확인되는 최근 공개·개봉·제작 등 소식'))
        if published is None:
            details.append(('날짜 확인', '과거 자료에 기사 발행일이 없어 저장한 날짜를 표시했어요.'))
    if '복원' in str(row.get('quality_notes', '')):
        result['verification_note'] = '표시된 집계 기간의 저장 기록입니다. 공식 페이지의 현재 수치와 내용은 달라질 수 있어요.'
    return result
