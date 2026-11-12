"""Synthetic engine contract tests. These do not establish musical accuracy or GPU acceptance."""
import hashlib,importlib.util,json,pathlib,socket,tempfile,types,unittest,wave
from unittest import mock

SPEC=importlib.util.spec_from_file_location('generic_russian_worker',pathlib.Path(__file__).with_name('gigaam_model_worker.py'))
worker=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(worker)

class GenericRecognitionContract(unittest.TestCase):
    def test_chunks_cover_the_original_audio_with_bounded_contiguous_windows(self):
        import numpy as np
        samples=np.zeros(60*16000,dtype=np.float32)
        ranges=list(worker.chunk_ranges(samples))
        self.assertEqual(ranges[0][0],0);self.assertEqual(ranges[-1][1],len(samples))
        self.assertTrue(all(0<b-a<=23.5*16000 for a,b in ranges))
        self.assertTrue(all(a[1]==b[0] for a,b in zip(ranges,ranges[1:])))

    def run_fixture(self,*,tamper=False,reference=''):
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory);cache=root/'models';cache.mkdir()
            for name in ('v3_e2e_rnnt.ckpt','v3_e2e_rnnt_tokenizer.model'):(cache/name).write_bytes(b'synthetic asset')
            digest=hashlib.sha256(b'synthetic asset').hexdigest()
            if tamper:(cache/'v3_e2e_rnnt.ckpt').write_bytes(b'tampered')
            request={'operation':'transcribe','language':'ru','text':reference,'model_directory':str(cache),
                     'cuda_dll_directory':str(root)}
            (root/'request.json').write_text(json.dumps(request),encoding='utf-8')
            with wave.open(str(root/'audio.wav'),'wb') as audio:
                audio.setparams((1,2,16000,0,'NONE','NONE'));audio.writeframes(b'\0\0'*(60*16000))
            calls=[]
            def transcribe(path,word_timestamps):
                self.assertTrue(word_timestamps);calls.append(path)
                words=[types.SimpleNamespace(text='добрый',start=.1,end=.3),types.SimpleNamespace(text='день',start=.4,end=.7)]
                return types.SimpleNamespace(text='добрый день',words=words)
            load=mock.Mock(return_value=types.SimpleNamespace(transcribe=transcribe))
            fake=types.SimpleNamespace(load_model=load)
            with mock.patch.object(worker,'WEIGHT_SHA256',digest),mock.patch.object(worker,'VOCABULARY_SHA256',digest), \
                 mock.patch.object(worker.sys,'argv',['worker',str(root)]),mock.patch.dict(worker.sys.modules,{'gigaam':fake}), \
                 mock.patch.object(worker.importlib.metadata,'version',return_value='0.2.0'), \
                 mock.patch.object(socket.socket,'connect',socket.socket.connect), \
                 mock.patch.object(worker.os,'add_dll_directory',return_value=types.SimpleNamespace(close=lambda:None),create=True):
                # The worker's network guard is restored by the patch scope.
                worker.main()
            result=json.loads((root/'result.json').read_text(encoding='utf-8'))
            return result,calls,load

    def test_real_model_word_positions_are_offset_without_fake_confidence(self):
        result,calls,load=self.run_fixture()
        self.assertEqual(len(calls),4);self.assertEqual(len(result['words']),8)
        self.assertEqual([segment['start'] for segment in result['segments']],[0,18,36,54])
        self.assertEqual([result['words'][i]['start'] for i in (0,2,4,6)],[.1,18.1,36.1,54.1])
        self.assertFalse(result['word_probabilities']);self.assertFalse(result['confidence_available'])
        self.assertFalse(result['automatic_acceptance'])
        self.assertTrue(all('probability' not in word for word in result['words']))
        self.assertEqual(result['words'],[w for s in result['segments'] for w in s['words']])
        self.assertEqual(load.call_args.kwargs['device'],'cuda')

    def test_modified_weights_are_rejected_before_model_loading(self):
        with self.assertRaisesRegex(ValueError,'Pinned model asset mismatch'):self.run_fixture(tamper=True)

    def test_reference_text_cannot_turn_independent_recognition_into_prompted_output(self):
        with self.assertRaisesRegex(ValueError,'independent recognition profile'):self.run_fixture(reference='invented reference')

if __name__=='__main__':unittest.main()
