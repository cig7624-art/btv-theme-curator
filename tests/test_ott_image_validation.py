"""Full image verification counts actual distinct public URLs, not reused cards."""
import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('ott_images',Path(__file__).resolve().parents[1]/'scripts/check_ott_images.py')
images=importlib.util.module_from_spec(spec);spec.loader.exec_module(images)

class ImageValidationTests(unittest.TestCase):
    def snapshot(self):
        work=lambda n:{'title':str(n),'image_url':f'https://images.example.com/{n}.jpg'}
        return {'platforms':[{'id':'tving','promotions':[work(1)],'sections':[{'works':[work(1),work(2),work(3),work(4)]}],'public_lists':[]},
                            {'id':'wavve','promotions':[work(5)],'sections':[{'works':[work(5),work(6),{'title':'bad','image_url':'javascript:alert(1)'},{'title':'local','image_url':'http://127.0.0.1/secret'}]}],'public_lists':[]}]}

    def test_default_samples_two_distinct_public_images_per_service(self):
        result=images.image_jobs(self.snapshot())
        self.assertEqual([(platform,item['title']) for platform,item in result],[('tving','1'),('tving','2'),('wavve','5'),('wavve','6')])

    def test_all_mode_checks_every_saved_unique_public_image_and_no_invalid_url(self):
        result=images.image_jobs(self.snapshot(),all_images=True)
        self.assertEqual([item['title'] for _,item in result],['1','2','3','4','5','6'])
