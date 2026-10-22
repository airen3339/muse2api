"""Run the actual submit selector in an isolated Chromium, loopback fixture only."""
import json, os, shutil, socket, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import MuseEngine

HTML = b'''<!doctype html><html><body><main>
<div id="composer" class="cursor-text"><div><div><textarea></textarea></div></div>
<div><span><button aria-label="&#20659;&#36865;">send</button></span></div></div>
</main><script>
window.clicks=0; window.keys=0;
document.querySelector('textarea').addEventListener('keydown',()=>window.keys++);
document.querySelector('button').onclick=()=>{window.clicks++;};
</script></body></html>'''
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
        self.end_headers();self.wfile.write(HTML)
    def log_message(self,*args):pass

chromium=os.environ.get('MUSE2API_CHROMIUM') or next((shutil.which(n) for n in ('chromium','chromium-browser','google-chrome') if shutil.which(n)),None)
assert chromium, 'Chromium required: this test must not be counted as passed without running.'
with tempfile.TemporaryDirectory(prefix='muse-chat-browser-') as temp:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    cfg=SimpleNamespace(chromium=chromium,cdp_port=port,home_dir=temp,extra_path='',profile_dir=temp+'/profile',data_dir=temp,download_dir=temp+'/downloads')
    Path(cfg.download_dir).mkdir();engine=MuseEngine(cfg)
    results=[]
    try:
        engine.start();page=engine._open_page();engine.page=page
        url='http://127.0.0.1:'+str(server.server_address[1])+'/thread/new'
        def fresh():
            old_epoch=page.js('performance.timeOrigin')
            page.send('Page.navigate',{'url':url},timeout=3)
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if page.js("document.readyState==='complete' && !!document.querySelector('textarea') && location.pathname==='/thread/new' && performance.timeOrigin !== "+str(old_epoch),timeout=1):break
                time.sleep(.05)
            else:raise AssertionError('fixture load timeout')
            engine.chat_submission_started=False
            return engine._prepare_chat_submission()
        epoch=fresh();started=time.monotonic()
        assert engine._send_chat_once('FTL-繁体输入验证',epoch)=='dispatched'
        assert page.js('window.clicks')==1 and page.js('window.keys')==0
        assert engine.chat_submission_started and time.monotonic()-started<2
        results.append('traditional send clicked once without keyboard fallback')
        epoch=fresh();prompt='complete-😀-\n' * 12000
        assert engine._send_chat_once(prompt,epoch)=='dispatched'
        assert page.js('document.querySelector("textarea").value')==prompt
        assert page.js('window.clicks')==1
        results.append('large unicode prompt preserved and submitted once')
        epoch=fresh()
        page.js("document.querySelector('button').setAttribute('aria-disabled','true')")
        engine._chat_page_state('x',epoch,'fill')
        state=engine._chat_page_state('x',epoch,'submit')
        assert not state['dispatched'] and page.js('window.clicks')==0
        results.append('aria-disabled prevents dispatch')
        epoch=fresh()
        page.js("document.querySelector('button').setAttribute('aria-label','Resend');document.querySelector('button').innerText='Resend';document.body.insertAdjacentHTML('beforeend','<button aria-label=Send onclick=window.clicks++>Send</button>')")
        engine._chat_page_state('x',epoch,'fill')
        assert not engine._chat_page_state('x',epoch,'submit')['dispatched']
        assert page.js('window.clicks')==0
        results.append('Resend and external Send are not used')
        epoch=fresh()
        engine._chat_page_state('x',epoch,'fill')
        assert not engine._chat_page_state('x',epoch+1,'submit')['dispatched']
        assert page.js('window.clicks')==0
        results.append('page epoch drift prevents dispatch')
        epoch=fresh()
        page.js("document.querySelector('main').insertAdjacentHTML('afterbegin','<div class=\"hatch-chat-groupable-bubble hatch-agent-bubble-bg\">Connecting... Failed to send Still sending</div>')")
        state=engine._chat_page_state()
        assert not any(state[k] for k in ('connecting','failed','pending')) and state['assistants']==1
        results.append('conversation words are not connection/error notices')
        page.js("document.querySelector('main').insertAdjacentHTML('beforeend','<div role=alert>傳送失敗</div>')")
        assert engine._chat_page_state()['failed']
        results.append('localized visible UI error detected')
        epoch=fresh();engine._ensure_local().chat_expected_epoch=epoch
        page.js("document.querySelector('main').insertAdjacentHTML('afterbegin','<div class=\"hatch-chat-groupable-bubble chat-user-bubble\">prompt</div><div class=\"hatch-chat-groupable-bubble hatch-agent-bubble-bg\">answer</div>')")
        assert engine._poll_chat()==(1,'answer',False)
        engine._ensure_local().chat_expected_epoch=epoch+1
        try:engine._poll_chat()
        except Exception as error:assert '重新加载' in str(error)
        else:raise AssertionError('foreign document text accepted')
        results.append('answer and document identity checked in same browser snapshot')
        print(json.dumps({'passed':True,'browser_cases':len(results),'checks':results},ensure_ascii=False))
    finally:
        engine.stop();server.shutdown();server.server_close();thread.join(2)
