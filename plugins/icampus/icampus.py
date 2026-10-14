"""Read-only iCampus daily collection. Credentials never enter summarizer input."""
from __future__ import annotations
import argparse, contextlib, fcntl, hashlib, html, json, os, re, signal, subprocess, sys, time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
from zoneinfo import ZoneInfo
import requests

from hermes_constants import get_hermes_home

PROJECT = get_hermes_home()
BASE = 'https://canvas.skku.edu'
TZ = ZoneInfo('Asia/Seoul')
SECRET = re.compile(r'password|cookie|authorization|token|secret|secure_params|verifier|signature|^sso$', re.I)


def now(): return datetime.now(TZ).isoformat()

def safe_url(value):
    if not isinstance(value,str): return ''
    u = urlsplit(value)
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode([(k,v) for k,v in parse_qsl(u.query) if not SECRET.search(k)]), ''))

def clean(value):
    if isinstance(value, dict): return {k:clean(v) for k,v in value.items() if not SECRET.search(k)}
    if isinstance(value, list): return [clean(v) for v in value]
    if isinstance(value, str):
        return re.sub(r'https?://[^\s<>"\']+', lambda m: safe_url(html.unescape(m[0])), value)
    return value

def write(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        os.chmod(tmp, 0o600); f.write(data); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)

def load(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).exists() else default

class Text(HTMLParser):
    def __init__(self): super().__init__(); self.parts=[]; self.links=[]; self.skip=0
    def handle_starttag(self, tag, attrs):
        if tag in ('script','style'): self.skip += 1
        if tag in ('p','div','br','li','tr','h1','h2','h3'): self.parts.append('\n')
        a=dict(attrs)
        if tag=='a' and a.get('href'): self.links.append(a['href'])
        if tag=='img' and a.get('src'): self.links.append(a['src'])
    def handle_endtag(self,tag):
        if tag in ('script','style'): self.skip=max(0,self.skip-1)
    def handle_data(self,data):
        if not self.skip:self.parts.append(data)

def plain(s):
    p=Text();p.feed(s or '');return re.sub(r'\n\s*\n+', '\n\n', ''.join(p.parts)).strip()

class CollectionError(Exception): pass

class Client:
    def __init__(self, session, deadline): self.session=session;self.deadline=deadline;self.audit=[]
    def get(self,url,params=None):
        url=urljoin(BASE,url)
        if urlsplit(url).hostname!='canvas.skku.edu':raise CollectionError('unexpected API host')
        for attempt in range(3):
            if time.monotonic()>=self.deadline:raise CollectionError('collection time budget exceeded')
            try:
                r=self.session.get(url,params=params,timeout=(10,30))
                if r.status_code==429 or r.status_code>=500:
                    if attempt<2:time.sleep(1+attempt);continue
                if r.status_code!=200:raise CollectionError('HTTP '+str(r.status_code))
                data=json.loads(r.text.removeprefix('while(1);'))
                self.audit.append({'url':safe_url(r.url),'status':r.status_code,'count':len(data) if isinstance(data,list) else None})
                return data,r
            except (requests.RequestException,ValueError) as e:
                if attempt==2:raise CollectionError(type(e).__name__) from None
                time.sleep(1+attempt)
        raise CollectionError('retry limit')
    def all(self,url,params=None):
        params={'per_page':100,**(params or {})};result=[];seen=set()
        for _ in range(100):
            if url in seen:raise CollectionError('pagination cycle')
            seen.add(url);data,r=self.get(url,params)
            if not isinstance(data,list):raise CollectionError('expected list')
            result.extend(data);url=r.links.get('next',{}).get('url');params=None
            if not url:return result
        raise CollectionError('pagination limit')

def current_courses(courses, at=None):
    at=at or datetime.now(TZ)
    candidates=[]
    for c in courses:
        term=c.get('term') or {};match=re.fullmatch(r'(\d{4})년 ([12])학기',term.get('name',''))
        if not match:continue
        start=term.get('start_at')
        if start and datetime.fromisoformat(start.replace('Z','+00:00'))>at:continue
        key=tuple(map(int,match.groups()))
        if key[0]!=at.year:continue
        if not start and key[1]>(2 if at.month>=8 else 1):continue
        candidates.append((key,c))
    if not candidates:raise CollectionError('현재 학기 정규 과목을 확인할 수 없음')
    latest=max(k for k,c in candidates)
    return [c for k,c in candidates if k==latest]

@contextlib.contextmanager
def authenticated(root, credentials, fresh=False):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        state=root/'session.json'
        context=browser.new_context(storage_state=str(state) if state.exists() and not fresh else None)
        page=context.new_page();page.set_default_timeout(30000)
        page.goto(BASE+'/',wait_until='domcontentloaded',timeout=45000)
        if not page.locator('#global_nav_dashboard_link').count():
            creds=load(credentials)
            if not creds or not creds.get('username') or not creds.get('password'):raise CollectionError('로그인 정보 입력 필요')
            page.goto('https://icampus.skku.edu/login',wait_until='networkidle',timeout=45000)
            if page.locator('#userid').count():
                page.locator('#userid').fill(creds['username']);page.locator('#password').fill(creds['password'])
                page.get_by_role('button',name='LOGIN',exact=True).click()
                page.wait_for_timeout(1500)
            page.wait_for_load_state('networkidle',timeout=45000)
            page.goto(BASE+'/',wait_until='domcontentloaded',timeout=45000)
            if not page.locator('#global_nav_dashboard_link').count():
                raise CollectionError('로그인 확인 실패: 추가 인증/CAPTCHA/계정 상태를 사용자 확인 필요')
        write(state,context.storage_state())
        session=requests.Session()
        for c in context.cookies():session.cookies.set(c['name'],c['value'],domain=c['domain'],path=c['path'])
        try:yield context,page,session
        finally:browser.close()

def module_data(page,cid):
    captured={}
    def capture(response):
        path=urlsplit(response.url).path
        prefix=f'/learningx/api/v1/courses/{cid}/'
        if path in (prefix+'modules',prefix+'lessons') and response.status==200:
            try:
                captured[path.rsplit('/',1)[1]]=response.json()
            except Exception:pass
    page.on('response',capture)
    try:
        page.goto(f'{BASE}/courses/{cid}',wait_until='networkidle',timeout=45000)
        links=page.locator('#section-tabs a').evaluate_all('(es)=>es.map(e=>({text:e.innerText,href:e.href}))')
        target=next((x['href'] for x in links if x['text'].strip() in ('강의콘텐츠','Lecture Contents')),None)
        if not target: return {'modules':[], 'lessons':[], 'menu':links, 'absent':True}
        page.goto(target,wait_until='networkidle',timeout=45000)
        if 'modules' not in captured:raise CollectionError('강의 목록 응답 미확인')
        frame=next((f for f in page.frames if '/modulebuilder' in f.url),None)
        if frame:
            button=frame.locator('.xnmb-all_fold-btn')
            if button.count() and ('펼치기' in button.inner_text() or 'Expand' in button.inner_text() or 'Open all' in button.inner_text()):
                button.dispatch_event('click')
            titles=frame.locator('.xnmb-module_item-left-title').all_text_contents()
            captured['screen_titles']=[t.strip() for t in titles]
            expected=[str(i.get('title','')).strip() for m in captured['modules'] for i in m.get('module_items',[])]
            from collections import Counter
            captured['screen_matches_api']=Counter(captured['screen_titles'])==Counter(expected)
            if not captured['screen_matches_api']:raise CollectionError('강의 화면과 API 항목 목록 불일치')
        else:raise CollectionError('강의 목록 프레임 미확인')
        captured['menu']=links
        return captured
    finally:page.remove_listener('response',capture)

def board_data(page,cid,menu):
    target=next((m['href'] for m in menu if m['text'].strip() in ('게시판','Board')),None)
    if not target:return [],{'course_id':cid,'kind':'board','count':0,'status':'menu_absent'}
    base=f'/learningx/api/v1/learningx_board/courses/{cid}/boards'
    captured={}
    def capture(response):
        if urlsplit(response.url).path==base and response.status==200:
            captured['boards']=response.json()
            captured['headers']={k:v for k,v in response.request.all_headers().items() if k not in ('host','content-length','cookie')}
    page.on('response',capture)
    try:
        page.goto(target,wait_until='networkidle',timeout=45000)
        if 'boards' not in captured:raise CollectionError('게시판 목록 미확인')
        result=[]
        for board in captured['boards']:
            posts=[];seen=set();expected=board.get('total_post_count',0)
            for number in range(1,101):
                r=page.context.request.get(BASE+base+f'/{board["id"]}/posts',params={'page':number,'per_page':100},headers=captured['headers'],timeout=30000)
                if r.status!=200:raise CollectionError('게시판 HTTP '+str(r.status))
                payload=r.json()
                rows=payload.get('items') if isinstance(payload,dict) else payload
                if not isinstance(rows,list):raise CollectionError('게시판 목록 형식 변경: '+str(list(rows)[:8] if isinstance(rows,dict) else type(rows).__name__))
                fresh=[x for x in rows if x.get('id') not in seen]
                posts.extend(fresh);seen.update(x.get('id') for x in fresh)
                if len(posts)>=expected:break
                if not fresh:raise CollectionError('게시판 전체 항목 수 불일치')
            else:raise CollectionError('게시판 페이지 한도')
            result.append({'id':board['id'],'title':board['title'],'expected':expected,'posts':[
                {k:v for k,v in x.items() if k in ('id','title','created_at','updated_at','is_notice','is_secret','attachments')} for x in posts]})
        return result,{'course_id':cid,'kind':'board','count':sum(len(b['posts']) for b in result),'boards':len(result),'status':'ok'}
    finally:page.remove_listener('response',capture)

def item_record(cid,kind,x):
    body=(x.get('description',x.get('message',x.get('body',''))) or '') if kind in ('announcement','assignment','quiz','discussion','syllabus') else ''
    r={'id':f'{cid}:{kind}:{x.get("id",x.get("module_item_id",x.get("page_id",x.get("url"))))}', 'course_id':cid,'kind':kind,
       'title':x.get('title',x.get('name',x.get('display_name',x.get('filename','')))), 'body':plain(body), 'body_html':clean(body),
       'url':safe_url(x.get('html_url',x.get('url',f'{BASE}/courses/{cid}'))),
       'due_at':x.get('due_at'),'unlock_at':x.get('unlock_at'),'lock_at':x.get('lock_at'),
       'updated_at':x.get('updated_at'),'posted_at':x.get('posted_at',x.get('created_at')),
       'locked':bool(x.get('locked_for_user')), 'site_status':(x.get('submission') or {}).get('workflow_state','unknown'),
       'submission_types':x.get('submission_types'), 'attachments':clean(x.get('attachments',[])),
       'category':{'announcement':'공지','assignment':'과제','quiz':'시험·퀴즈','discussion':'토론','file':'자료','page':'페이지','syllabus':'수업 계획서','board':'게시판'}.get(kind,kind)}
    if kind=='file':r['url']=f'{BASE}/courses/{cid}/files/{x["id"]}'
    if not r['url']:r['url']=f'{BASE}/courses/{cid}'
    return r

def module_records(cid,modules):
    records=[]
    for m in modules:
        for x in m.get('module_items',[]):
            d=x.get('content_data') or {};content=d.get('item_content_data') or {}
            r=item_record(cid,'learning',x);r.update({'body':plain(d.get('description','')),'body_html':clean(d.get('description','')),
              'week':m.get('title'),'due_at':d.get('due_at'),'unlock_at':d.get('unlock_at'),'lock_at':d.get('lock_at'),
              'updated_at':d.get('updated_at'),'locked':d.get('opened') is False,
              'site_status':'completed' if x.get('completed') is True else 'incomplete' if x.get('completed') is False else 'unknown',
              'attendance_status':x.get('attendance_status'),'content':clean({k:v for k,v in content.items() if k not in ('content','thumbnail_url')}),
              'content_type':content.get('content_type',d.get('item_content_type',x.get('content_type'))),
              'text_read_status':'metadata_only'})
            typ=r['content_type']
            r['category']=('강의' if typ in ('everlec','movie','screenlecture','video','audio') else
                           '실시간 강의' if typ in ('zoom','video_conference') else
                           '과제' if typ=='assignment' else '시험·퀴즈' if typ=='quiz' else '자료')
            r['body']='';r['body_html']=''
            r['content_id']=x.get('content_id')
            if x.get('content_type') in ('assignment','quiz'):
                endpoint='assignments' if x['content_type']=='assignment' else 'quizzes'
                r['url']=f'{BASE}/courses/{cid}/{endpoint}/{x["content_id"]}'
            else:r['url']=f'{BASE}/courses/{cid}/external_tools/297'
            records.append(r)
    return records

def compare(old,new):
    previous={x['id']:x for x in old};present={x['id']:x for x in new};changes=[]
    for k,x in present.items():
        if k not in previous:changes.append({'id':k,'type':'new'})
        elif x!=previous[k]:
            fields=[f for f in set(x)|set(previous[k]) if x.get(f)!=previous[k].get(f)]
            changes.append({'id':k,'type':'modified','fields':sorted(fields),'old_due_at':previous[k].get('due_at'),'new_due_at':x.get('due_at')})
    for k in previous.keys()-present.keys():changes.append({'id':k,'type':'not_seen','meaning':'완료로 추정하지 않음'})
    return changes

def task_items(items, previous=()):
    current={}; canonical=set()
    for x in items:
        if x['kind'] in ('assignment','quiz'):
            canonical.add((x['course_id'],x['kind'],x['id'].rsplit(':',1)[-1]))
    for x in items:
        if x['kind'] not in ('assignment','quiz','learning'):continue
        if x['kind']=='learning' and (x['course_id'],x.get('content_type'),str(x.get('content_id'))) in canonical:continue
        current[x['id']]={**x,'observation':'seen'}
    for x in previous:
        if x['id'] not in current:current[x['id']]={**x,'observation':'not_seen'}
    return list(current.values())

def local_date(value):
    if not value:return '—'
    return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(TZ).strftime('%Y-%m-%d %H:%M')

def md(value):return str(value or '').replace('|',r'\|').replace('\n',' ')

def render(data,changes):
    names={c['id']:c['name'] for c in data['courses']};items=data['items']
    lines=['# iCampus 일일 확인',f'확인 시각: {data["checked_at"]} · 현재 학기 정규 과목 {len(names)}개 · {data["status"]}',
           '', '자료·동영상은 이름과 메타데이터만 확인했습니다. 공지·과제·퀴즈 안내는 본문을 포함합니다.',
           '', '## 가까운 마감 (7일)', '']
    at=datetime.fromisoformat(data['checked_at']);count=0
    for x in sorted(task_items(items),key=lambda x:x.get('due_at') or '9999'):
        if not x.get('due_at') or x['site_status'] in ('completed','submitted','graded'):continue
        due=datetime.fromisoformat(x['due_at'].replace('Z','+00:00')).astimezone(TZ)
        if 0<=(due-at).total_seconds()<=7*86400:
            lines.append(f'- {due:%m-%d %H:%M} · {names[x["course_id"]]} · [{md(x["title"])}]({x["url"]}) · {x["category"]}');count+=1
    if not count:lines.append('확인된 7일 이내 미완료 마감 없음.')
    lines.extend(['','## 변경 사항','',f'신규 {sum(c["type"]=="new" for c in changes)}개 · 수정 {sum(c["type"]=="modified" for c in changes)}개 · 목록에서 미확인 {sum(c["type"]=="not_seen" for c in changes)}개'])
    change_ids={x['id']:x['type'] for x in changes}
    for cid,name in names.items():
        lines.extend(['',f'## {name}',f'{BASE}/courses/{cid}',''])
        menus=data.get('menus',{}).get(str(cid),[])
        if menus:lines.extend(['메뉴: '+ ' · '.join(f'[{md(m["text"])}]({safe_url(m["href"])})' for m in menus),''])
        for category in ('강의','실시간 강의','자료','과제','시험·퀴즈','페이지','수업 계획서','토론','게시판','공지'):
            rows=[i for i in items if i['course_id']==cid and i['category']==category]
            lines.extend([f'### {category} ({len(rows)})',''])
            if not rows:lines.append('표시된 항목 없음. 카테고리 접근 여부는 아래 수집 범위를 참고하세요.');lines.append('');continue
            lines.extend(['| 이름 | 주차 | 마감 (한국시간) | 공개 시작 | 상태 | 변경 |','|---|---|---|---|---|---|'])
            for x in rows:
                status='공개 전/접근 제한' if x['locked'] else x['site_status']
                lines.append(f'| [{md(x["title"])}]({x["url"]}) | {md(x.get("week"))} | {local_date(x.get("due_at"))} | {local_date(x.get("unlock_at"))} | {status} | {change_ids.get(x["id"],"없음")} |')
            lines.append('')
            for x in rows:
                if x.get('body'):
                    lines.extend([f'**[{md(x["title"])}]({x["url"]})**', '',x['body'],''])
                if x.get('posted_at'):lines.extend(['게시: '+local_date(x['posted_at']),''])
                for a in x.get('attachments',[]):
                    lines.append(f'- 첨부: [{md(a.get("display_name",a.get("filename","첨부자료")))}]({safe_url(a.get("url",""))}) (본문 미수집)')
    lines.extend(['','## 수집 범위와 예외',''])
    for c in data['coverage']:
        detail=c.get('status','ok');count=c.get('count',c.get('modules',0))
        lines.append(f'- {names.get(c["course_id"],c["course_id"])} / {c["kind"]}: {count}개 · {detail}')
    for e in data['gaps']:lines.append(f'- {e.get("id",e.get("course_id",""))}: {e["reason"]}')
    return '\n'.join(lines)+'\n'

def collect(root,credentials,fresh=False,budget=900):
    at=now();run_id=datetime.now(TZ).strftime('%Y%m%dT%H%M%S%f');snapshot=root/'snapshots'/at[:10]/run_id
    data={'checked_at':at,'courses':[],'items':[],'gaps':[],'coverage':[],'menus':{}}; fatal=[]
    with authenticated(root,credentials,fresh) as (context,page,session):
        client=Client(session,time.monotonic()+budget)
        all_courses=client.all('/api/v1/courses',{'include[]':'term','enrollment_state':'active'})
        courses=current_courses(all_courses)
        data['courses']=[{'id':c['id'],'name':c['name'],'term':c['term']['name'],'url':f'{BASE}/courses/{c["id"]}'} for c in courses]
        write(snapshot/'course-inventory.json',clean(all_courses))
        for c in courses:
            cid=c['id'];print('collect',cid,flush=True)
            menu=None
            try:
                module=module_data(page,cid)
                menu=module['menu'];data['menus'][str(cid)]=menu
                write(snapshot/f'{cid}-learning.json',clean(module))
                data['items'].extend(module_records(cid,module['modules']))
                data['coverage'].append({'course_id':cid,'kind':'learning','modules':len(module['modules']), 'count':sum(len(m.get('module_items',[])) for m in module['modules']), 'status':'menu_absent' if module.get('absent') else 'ok'})
            except Exception as e:
                data['gaps'].append({'course_id':cid,'reason':'강의 목록: '+(str(e) if isinstance(e,CollectionError) else type(e).__name__)});fatal.append(f'{cid}:learning')
            if menu is not None:
                try:
                    boards,coverage=board_data(page,cid,menu)
                    write(snapshot/f'{cid}-boards.json',clean(boards));data['coverage'].append(coverage)
                    for board in boards:
                        for x in board['posts']:
                            item=item_record(cid,'board',{**x,'html_url':f'{BASE}/courses/{cid}/external_tools/303'})
                            item['board']=board['title'];data['items'].append(item)
                except Exception as e:
                    data['gaps'].append({'course_id':cid,'reason':'게시판: '+(str(e) if isinstance(e,CollectionError) else type(e).__name__)});fatal.append(f'{cid}:board')
            for kind,endpoint,params in [('announcement','discussion_topics',{'only_announcements':'true'}),('assignment','assignments',{'include[]':'submission'}),('quiz','quizzes',{}),('page','pages',{}),('file','files',{}),('discussion','discussion_topics',{})]:
                try:
                    rows=client.all(f'/api/v1/courses/{cid}/{endpoint}',params)
                    if kind=='discussion':rows=[r for r in rows if not r.get('is_announcement') and r.get('discussion_type') is not None]
                    write(snapshot/f'{cid}-{kind}.json',clean(rows))
                    data['items'].extend(item_record(cid,kind,x) for x in rows)
                    data['coverage'].append({'course_id':cid,'kind':kind,'count':len(rows),'pagination':'all Link pages'})
                except CollectionError as e:
                    menu_has_category=menu is None or any(urlsplit(m['href']).path.endswith('/'+endpoint) for m in menu)
                    if str(e) in ('HTTP 401','HTTP 403','HTTP 404') and not menu_has_category:
                        data['coverage'].append({'course_id':cid,'kind':kind,'count':0,'status':'not_exposed_by_course','http_status':str(e)})
                    else:
                        data['gaps'].append({'course_id':cid,'reason':kind+': '+str(e)});fatal.append(f'{cid}:{kind}')
            try:
                info,_=client.get(f'/api/v1/courses/{cid}',{'include[]':'syllabus_body'})
                syllabus=info.get('syllabus_body') or ''
                if syllabus:
                    data['items'].append(item_record(cid,'syllabus',{'id':cid,'title':'수업 계획서','body':syllabus,'html_url':f'{BASE}/courses/{cid}/assignments/syllabus'}))
                data['coverage'].append({'course_id':cid,'kind':'syllabus','count':int(bool(syllabus)),'status':'ok'})
            except CollectionError as e:
                data['gaps'].append({'course_id':cid,'reason':'syllabus: '+str(e)});fatal.append(f'{cid}:syllabus')
            write(snapshot/'checkpoint.json',clean(data))
        write(snapshot/'requests.json',client.audit)
    data=clean(data)
    data['status']='partial' if fatal else 'collected';data['incomplete_sections']=fatal
    write(snapshot/'data.json',clean(data))
    previous=load(root/'last-collected.json',{'items':[]})
    changes=compare(previous['items'],data['items']);report=render(data,changes)
    write(snapshot/'report.md',report)
    write(root/'runs'/f'{run_id}.json',{'started_at':at,'finished_at':now(),'status':data['status'],'snapshot':str(snapshot),'incomplete_sections':fatal})
    if not fatal:
        write(root/'last-collected.json',data);write(root/'changes.json',changes)
        write(root/'tasks.json',task_items(data['items'],load(root/'tasks.json',[])))
        write(root/'reports'/f'{at[:10]}.md',report);write(root/'latest.md',report)
    return snapshot,data

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=PROJECT/'state/icampus');p.add_argument('--credentials',type=Path,default=PROJECT/'state/icampus-credentials.json');p.add_argument('--fresh-login',action='store_true');p.add_argument('--budget',type=int,default=900)
    args=p.parse_args();os.umask(0o077);args.root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with open(args.root/'run.lock','a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:print('already running');return 0
        def timeout(*_):raise CollectionError('전체 실행 시간 제한 초과')
        signal.signal(signal.SIGALRM,timeout);signal.alarm(args.budget)
        try:
            snapshot,data=collect(args.root,args.credentials,args.fresh_login,args.budget)
            print(json.dumps({'snapshot':str(snapshot),'status':data['status'],'courses':len(data['courses']),'items':len(data['items'])},ensure_ascii=False));return 0 if data['status']=='collected' else 1
        except Exception as e:
            reason=str(e) if isinstance(e,CollectionError) else type(e).__name__
            write(args.root/'runs'/f'failure-{datetime.now(TZ):%Y%m%dT%H%M%S}.json',{'at':now(),'status':'failed','reason':reason})
            print('collection failed:',reason);return 1
        finally:signal.alarm(0)

if __name__=='__main__':sys.exit(main())
