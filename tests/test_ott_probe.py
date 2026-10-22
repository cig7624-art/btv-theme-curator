"""Public evidence must omit transient identity and classify normal pages fairly."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('ott_probe', Path(__file__).resolve().parents[1] / 'scripts/probe_ott_public.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

class ProbeTests(unittest.TestCase):
    def test_nested_transient_identity_fields_and_serialized_values_are_removed(self):
        value = {'data': {'title': '공개 홈', 'securetoken': 'secret-test', 'guid': 'identity-test', 'ip': '192.0.2.1',
                          'state': '{"accessToken":"secret-test", "title":"공개 작품"}'}}
        result = probe.redact(value)
        self.assertEqual(result['data']['title'], '공개 홈')
        for token in ['secret-test', 'identity-test', '192.0.2.1']:
            self.assertNotIn(token, str(result))
        self.assertIn('공개 작품', str(result))

    def test_urls_strip_identity_but_keep_public_catalog_parameters(self):
        result = probe.safe_url('https://apis.wavve.com/v1/catalog?uicode=A1&securetoken=secret-test&guid=identity-test&ip=192.0.2.1&utm_source=test')
        self.assertEqual(result, 'https://apis.wavve.com/v1/catalog?uicode=A1')

    def test_normal_footer_recaptcha_notice_is_not_a_block(self):
        text = '지금 뜨는 콘텐츠 사이버펑크 원피스 가입하기 이 페이지는 Google reCAPTCHA로 보호됩니다.'
        self.assertEqual(probe.observation(text, 200, title='넷플릭스 대한민국'), 'public_document_received')
        self.assertEqual(probe.observation('Verify you are human', 200, title='Just a moment'), 'blocked_or_challenge')

    def test_html_removes_executable_content_and_keeps_css_image_delimiter(self):
        cleaned, _, _ = probe.clean_html('<main><script>window.token="secret-test"</script><div style="background-image:url(https://images.example.com/poster.jpg?v=1)">작품</div></main>')
        self.assertNotIn('secret-test', cleaned)
        self.assertIn('poster.jpg?v=1)', cleaned)
