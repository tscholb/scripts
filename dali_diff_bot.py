import http.server
import socketserver
import json
import urllib.request
import re
import base64
import subprocess
import tempfile
import os
import glob

PORT = 8080

def add_gerrit_auth(req, user, token):
    if user and token:
        auth_string = f"{user}:{token}"
        encoded_auth = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
        req.add_header('Authorization', f'Basic {encoded_auth}')

def parse_gerrit_pr(url, user, token):
    host_match = re.match(r"(https?://[^/]+(?:/gerrit)?)", url)
    if not host_match:
        return {"error": "Gerrit 호스트 주소를 파싱할 수 없습니다."}
    base_url = host_match.group(1)

    match = re.search(r"\+/(\d+)", url)
    if not match:
        match = re.search(r"(?:c/|#/c/)(\d+)", url)
    if not match:
        return {"error": "Gerrit URL에서 Change ID를 찾을 수 없습니다."}
        
    change_id = match.group(1)
    api_url = f"{base_url}/changes/{change_id}/detail?o=CURRENT_REVISION&o=CURRENT_COMMIT"
    
    try:
        req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
        add_gerrit_auth(req, user, token) # 인증 정보 추가
        
        with urllib.request.urlopen(req) as response:
            raw_data = response.read().decode('utf-8')
            if raw_data.startswith(")]}'"):
                raw_data = raw_data[4:].strip()
                
            data = json.loads(raw_data)
            repo = data.get('project', 'unknown').split('/')[-1]
            title = data.get('subject', 'No Title')
            author = data.get('owner', {}).get('name', 'Unknown')
            
            current_rev = data.get('current_revision', '')
            commit_info = data.get('revisions', {}).get(current_rev, {}).get('commit', {})
            after_commit = current_rev[:7]
            after_msg = commit_info.get('message', '').split('\n')[0]
            parents = commit_info.get('parents', [])
            before_commit = parents[0].get('commit', '')[:7] if parents else 'unknown'
            
            return {
                "success": True, "base_url": base_url, "change_id": change_id,
                "repo": repo, "before_commit": before_commit, "before_msg": "Base Commit",
                "after_commit": after_commit, "after_msg": after_msg,
                "title": f"[Gerrit] {title}", "author": author
            }
    except Exception as e:
        return {"error": f"Gerrit 파싱 실패 (Auth 에러 401/404 등):<br/><code>{api_url}</code><br/>{str(e)}"}

# === [신규] Gerrit SSH 연동 로직 ===
def parse_gerrit_pr_ssh(url, user):
    host_match = re.match(r"(https?://([^/]+)(?:/gerrit)?)", url)
    if not host_match: return {"error": "호스트 파싱 에러"}
    host = host_match.group(2) # review.tizen.org
    
    project_match = re.search(r"/(?:c|#/c)/(.*?)/\+?/(\d+)", url)
    if not project_match: return {"error": "프로젝트/ChangeID 파싱 에러"}
    project_name = project_match.group(1)
    change_id = project_match.group(2)
    
    ssh_target = f"{user}@{host}" if user else host
    cmd = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'BatchMode=yes', '-p', '29418', ssh_target, 'gerrit', 'query', '--format=JSON', '--current-patch-set', change_id]
    
    try:
        result = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode('utf-8')
        lines = result.strip().splitlines()
        
        if not lines:
            return {"error": "SSH Query 결과가 비어있습니다."}
            
        data = json.loads(lines[0])
        
        if 'rowCount' in data and data['rowCount'] == 0:
            return {"error": "SSH Query 결과가 없습니다. Change ID 오류 또는 접근 권한 없음."}
            
        repo = data.get('project', project_name).split('/')[-1]
        title = data.get('subject', 'No Title')
        author = data.get('owner', {}).get('name', 'Unknown')
        patch_set = data.get('currentPatchSet', {})
        after_commit = patch_set.get('revision', '')[:7]
        ref = patch_set.get('ref', '')
        
        return {
            "success": True, "auth_mode": "ssh",
            "ssh_target": ssh_target, "project": data.get('project', project_name), "ref": ref,
            "repo": repo, "before_commit": "Base", "before_msg": "Base",
            "after_commit": after_commit, "after_msg": title,
            "title": f"[Gerrit-SSH] {title}", "author": author
        }
    except Exception as e:
        return {"error": f"SSH Query 실패 (ssh-key 등록 확인 필요):<br/>명령어: <code>{' '.join(cmd)}</code><br/>{str(e)}"}

def fetch_gerrit_diff_ssh(ssh_target, project, ref):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.check_call(['git', 'init'], cwd=tmpdir, stdout=subprocess.DEVNULL)
            fetch_url = f"ssh://{ssh_target}:29418/{project}"
            env = os.environ.copy()
            env['GIT_SSH_COMMAND'] = 'ssh -o StrictHostKeyChecking=no -o BatchMode=yes'
            subprocess.check_call(['git', 'fetch', fetch_url, ref], cwd=tmpdir, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            diff_out = subprocess.check_output(['git', 'diff', 'FETCH_HEAD^', 'FETCH_HEAD'], cwd=tmpdir).decode('utf-8', errors='ignore')
            return [{"filename": "Gerrit Patchset (SSH fetched)", "patch": diff_out}]
    except Exception as e:
        return []
# ==================================

# === [신규] 로컬 샘플 스캐너 ===
def find_candidate_samples(diff_data):
    keywords = set()
    for d in diff_data:
        fname = os.path.basename(d.get('filename', ''))
        name = fname.split('.')[0].replace('-impl', '').replace('-internal', '')
        if len(name) > 3: keywords.add(name.lower())
        
    candidates = set()
    patterns = [
        "/home/sunghyun/workspace/dali-demo/examples/**/*.cpp",
        "/home/sunghyun/workspace/dali-ui/**/samples/**/*.cpp"
    ]
    
    all_samples = []
    for p in patterns:
        all_samples.extend(glob.glob(p, recursive=True))
        
    # 1. 변경된 코드명(키워드)과 일치하는 샘플 추출
    for s in all_samples:
        basename = os.path.basename(s).lower()
        if any(kw in basename for kw in keywords):
            candidates.add(basename)
            
    # 2. 매칭되는게 없으면 전체 목록 중 일부라도 제공하여 AI가 유추하게 함
    if not candidates:
        candidates = set([os.path.basename(s) for s in all_samples[:50]])
            
    return list(candidates)
# ==================================

def fetch_gerrit_diff(base_url, change_id, user, token):
    try:
        url = f"{base_url}/changes/{change_id}/revisions/current/patch"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        add_gerrit_auth(req, user, token)
        
        with urllib.request.urlopen(req) as resp:
            patch_text = base64.b64decode(resp.read()).decode('utf-8')
            return [{"filename": "Gerrit Patchset", "patch": patch_text}]
    except Exception as e:
        return []

def fetch_github_diff(owner, repo, base, head):
    try:
        url = f"https://api.github.com/repos/{owner}/{repo}/compare/{base}...{head}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return [{"filename": f.get('filename'), "patch": f.get('patch', '')} for f in data.get('files', [])]
    except:
        return []

def parse_github_pr(url):
    pattern = r"github\.com/([^/]+)/([^/]+)/pull/(\d+)"
    match = re.search(pattern, url)
    if not match: return {"error": "올바른 GitHub PR URL 형식이 아닙니다."}
    owner, repo, pr_number = match.groups()
    api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    try:
        req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            base_sha, head_sha = data['base']['sha'], data['head']['sha']
            pr_title, author = data['title'], data['user']['login']

        def get_commit_title(sha):
            try:
                c_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}"
                with urllib.request.urlopen(urllib.request.Request(c_url, headers={'User-Agent': 'Mozilla/5.0'})) as c_resp:
                    return json.loads(c_resp.read().decode('utf-8'))['commit']['message'].split('\n')[0]
            except: return ""

        return {
            "success": True, "owner": owner, "repo": repo,
            "before_commit": base_sha[:7], "before_msg": get_commit_title(base_sha),
            "after_commit": head_sha[:7], "after_msg": get_commit_title(head_sha),
            "title": pr_title, "author": author
        }
    except Exception as e:
        return {"error": f"GitHub 파싱 실패: {str(e)}"}

class RealGeminiAnalyzer:
    def analyze(self, meta_data, diff_data, ai_key, ai_model="gemini-3-flash", ai_engine="gemini", gauss_endpoint="", gauss_key="", gauss_model=""):
        if ai_engine == "gemini" and not ai_key:
            return """
            <div style="background: rgba(255, 77, 77, 0.1); border: 1px solid rgba(255, 77, 77, 0.3); padding: 20px; border-radius: 8px; margin-top: 15px;">
                <h4 style="margin:0 0 10px 0; color:#ff4d4d;">❌ AI 연동 오류: 연결된 API Key가 없습니다.</h4>
                <p style="margin:0; color:#ddd; font-size:14px;">우측 상단의 <b>[⚙️ 설정]</b> 창에서 AI API Key를 입력하거나 사내 Gauss AI로 전환해 주세요.</p>
            </div>
            """
            
        try:
            diff_str = "\\n".join([f"File: {d['filename']}\\n{d['patch']}" for d in diff_data])
            local_samples = find_candidate_samples(diff_data)
            samples_str = ", ".join(local_samples) if local_samples else "로컬 샘플 탐색 실패 (AI 자체 지식 활용 바람)"
            
            prompt = f"""
            You are an expert C++ reviewer for the Tizen DALi framework.
            Review the following git diff for repository '{meta_data['repo']}'.
            
            [지시사항 0: 리뷰어 페르소나 및 톤앤매너]
            당신은 Tizen DALi 프레임워크의 가장 엄격하고 냉철한 수석(Senior) C++ 아키텍트입니다.
            1. 무의미한 칭찬이나 감탄사("좋은 코드네요", "수고하셨습니다")는 절대 생략하세요.
            2. 코드의 잠재적 버그, 메모리 누수 위험, 예외 처리 누락, 동기화 문제, 성능 저하 가능성을 전문가 관점에서 집요하고 정확하게 짚어내세요.
            3. 문체는 객관적이고 단호하게 작성하세요. (예: "~하는 것이 좋습니다" 보다는 "~해야 합니다", "~위험이 존재합니다" 등)
            
            [지시사항 1: 로컬 샘플 매칭]
            다음은 사용자의 로컬 워크스페이스(dali-demo, dali-ui)에서 스캔된 실제 샘플 파일명 목록입니다:
            [{samples_str}]
            '🎯 추천 테스트 샘플' 섹션을 작성할 때, 허구의 샘플을 지어내지 말고 **반드시 위 목록에 존재하는 샘플들 중에서** 이번 변경점과 가장 연관 깊은 것을 골라서 이유와 함께 나열하세요.
            
            [지시사항 2: 다중 라인 리뷰 (중복 최소화)]
            '📝 라인별 코드 리뷰' 섹션을 작성할 때, 버그나 코멘트가 필요한 라인이 여러 곳이라면 **각기 다른 이슈마다 별도의 HTML <div> 블록을 생성하여 모두 보여주세요.** (딱 1건만 보여주고 끝내지 마세요.)
            단, 변수명 일괄 변경 등 완전히 동일하거나 유사한 패턴의 변경사항이 여러 번 등장할 경우, 모든 건을 보여줄 필요 없이 **대표적인 1건만 코드 블록으로 상세히 보여주고, 코멘트 하단에 "이 이슈는 Line 45, 60, 102 등에서도 동일하게 발생합니다."라고 라인 번호만 요약해서** 적어주세요.
            
            [지시사항 3: 종합 리뷰 점수 부여 (Gerrit 스타일)]
            전체적인 코드 맥락과 결함을 평가하여 아래 4가지 점수 중 하나를 엄격하게 부여하세요.
            <span style="color:#2ecc71; font-weight:bold;">+2 : 완벽! 반영 가능!</span> (결함 없음)
            <span style="color:#f1c40f; font-weight:bold;">+1 : 일부 minor한 수정이 필요하지만 검토 후 반영 가능!</span> (가벼운 개선점)
            <span style="color:#e67e22; font-weight:bold;">-1 : 수정되야할 문제가 있음. 수정후 다시 리뷰 필요</span> (버그 가능성)
            <span style="color:#ff4d4d; font-weight:bold;">-2 : 치명적인 결함이 보임. 절대 반영 불가</span> (크래시, 메모리 누수 등)
            
            [지시사항 4: 패치 전체 요약]
            '📝 라인별 코드 리뷰'를 시작하기 전에, 전체 Git Diff의 맥락을 분석하여 **이 패치가 무엇을 해결하려 하거나 어떤 기능을 추가하려는지** 2~3줄로 명확하게 요약하여 먼저 설명하세요.
            
            Output strictly in HTML format, structured EXACTLY with these sections:
            
            <div class="ai-score-section" style="background:rgba(255,255,255,0.05); padding:15px; border-radius:8px; margin-bottom:20px; text-align:center; font-size:18px; border:1px solid rgba(255,255,255,0.1);">
                🤖 <b>AI 종합 리뷰 판정:</b> ... (선택한 점수 태그를 그대로 넣으시고 1줄 요약 코멘트를 덧붙이세요) ...
            </div>
            <div class="ai-summary-section" style="background:rgba(255,255,255,0.05); padding:15px; border-radius:8px; margin-bottom:20px; border:1px solid rgba(255,255,255,0.1);">
                <h4 style="color:#00d2ff; margin-top:0;">📋 패치 전체 요약</h4>
                <p style="margin:0; font-size:14px; line-height:1.6;">... write the overall patch summary here ...</p>
            </div>
            <div class="ai-impact-section">
                <h4 style="color:#00d2ff;">🎯 추천 테스트 샘플 (로컬 매칭 기반)</h4>
                <ul>
                    <li>... list actual samples from the provided list ...</li>
                </ul>
            </div>
            <div class="ai-review-section">
                <h4 style="color:#00d2ff; margin-top:20px;">📝 라인별 코드 리뷰 (다중 리뷰)</h4>
                <!-- 이슈 1 -->
                <div style="background:#1e1e1e; padding:10px; border-radius:5px; font-family:monospace; font-size:13px; line-height:1.5; margin-bottom:10px;">
                    ... highlight the specific code changes and write your review comment below them with a robot emoji 🤖 ...
                </div>
                <!-- 이슈 2 (필요시 추가) -->
                <div style="background:#1e1e1e; padding:10px; border-radius:5px; font-family:monospace; font-size:13px; line-height:1.5; margin-bottom:10px;">
                    ... highlight the specific code changes and write your review comment below them with a robot emoji 🤖 ...
                </div>
            </div>
            
            Git Diff:
            {diff_str[:15000]}
            """
            
            if ai_engine == "gauss":
                url = gauss_endpoint.strip()
                if not url.endswith("/chat/completions"):
                    url = url.rstrip('/') + "/chat/completions"
                    
                payload = {
                    "model": gauss_model.strip() if gauss_model else "gauss",
                    "messages": [{"role": "user", "content": prompt}]
                }
                headers = {'Content-Type': 'application/json'}
                if gauss_key: headers['Authorization'] = f"Bearer {gauss_key}"
                
                req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers)
                with urllib.request.urlopen(req) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                    # OpenAI 호환 규격 파싱 (Langchain ChatOpenAI와 동일)
                    html_res = data['choices'][0]['message']['content']
                    return html_res.replace("```html", "").replace("```", "")
            else:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{ai_model}:generateContent?key={ai_key}"
                payload = {"contents": [{"parts": [{"text": prompt}]}]}
                req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(req) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                    html_res = data['candidates'][0]['content']['parts'][0]['text']
                    return html_res.replace("```html", "").replace("```", "")
                
        except Exception as e:
            error_detail = ""
            if hasattr(e, 'read'):
                try:
                    error_detail = e.read().decode('utf-8')
                except: pass
                
            # API 호출 실패 시 데모가 중단되지 않도록 Fallback(가상) 데이터 반환
            fallback_html = f"""
            <div style="background:rgba(243, 156, 18, 0.1); border:1px solid #f39c12; padding:15px; border-radius:8px; margin-bottom:15px;">
                <p style="margin:0; color:#f39c12;">⚠️ <b>AI API 서버 오류 발생 (에러코드: {str(e)})</b></p>
                <p style="margin:5px 0; font-size:12px; color:#ccc; word-break: break-all;"><b>상세 로그:</b> {error_detail}</p>
                <p style="margin:5px 0 0 0; font-size:12px; color:#ccc;"><b>안내:</b> 모델명({ai_model})이 틀렸거나, 권한이 없거나, 구글 서버 트래픽 문제일 수 있습니다.<br/>우측 상단 <b>[⚙️설정]</b>에서 <b>[연결 테스트 및 모델 불러오기]</b> 버튼을 눌러 본인의 API Key가 지원하는 실제 모델 목록을 불러온 뒤 다시 선택해 보세요.<br/>(현재는 원활한 UI/UX 시연을 위해 가상(Mock) 리뷰 데이터로 대체하여 표시합니다.)</p>
            </div>
            
            <div class="ai-impact-section">
                <h4 style="color:#00d2ff;">🎯 추천 테스트 샘플 (AI 추론) - <i>Fallback Mock</i></h4>
                <ul style="color:#ddd; font-size:14px;">
                    <li><a href="#" style="color:#3a7bd5;">lottie-animation-view-sample</a> (직접적인 변경 발생 추론)</li>
                    <li><a href="#" style="color:#3a7bd5;">image-view-sample</a> (상속/공통 인터페이스 변경의 여파)</li>
                </ul>
            </div>
            <div class="ai-review-section">
                <h4 style="color:#00d2ff; margin-top:20px;">📝 라인별 코드 리뷰 (Line-by-Line) - <i>Fallback Mock</i></h4>
                <div style="background:#1e1e1e; padding:10px; border-radius:5px; font-family:monospace; font-size:13px; line-height:1.5;">
                    <div style="color:#888;">// 파일: {meta_data['repo']}/internal/visuals/example-visual.cpp</div>
                    <div style="color:#ff4d4d; background:rgba(255,0,0,0.1);">- mVisualDirty = true;</div>
                    <div style="color:#2ecc71; background:rgba(0,255,0,0.1);">+ if (mVisualDirty) {{ FlushRender(); }}</div>
                    
                    <div style="margin-top:10px; padding:10px; background:rgba(58, 123, 213, 0.2); border-left:3px solid #3a7bd5; color:#fff;">
                        <b>🤖 AI 리뷰어 코멘트:</b><br/>
                        조건부 FlushRender() 호출은 성능 최적화 측면에서 훌륭합니다.<br/>
                        다만, Edge Case(예: 테마 변경 시)에 렌더링이 갱신되지 않는 회귀 버그(Regression)가 발생할 수 있습니다.
                    </div>
                </div>
            </div>
            """
            return fallback_html

ai_engine = RealGeminiAnalyzer()

HTML_CONTENT = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DALi AI Impact Analyzer</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600&display=swap" rel="stylesheet">
    <style>
        body { margin: 0; font-family: 'Inter', sans-serif; background: linear-gradient(135deg, #0f2027, #203a43, #2c5364); color: #fff; min-height: 100vh; display: flex; justify-content: center; align-items: center; position: relative; }
        .glass-panel { background: rgba(255, 255, 255, 0.05); backdrop-filter: blur(15px); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 20px; padding: 40px; width: 100%; max-width: 700px; box-shadow: 0 8px 32px 0 rgba(0,0,0,0.37); }
        h1 { text-align: center; margin-top: 0; font-weight: 600; background: -webkit-linear-gradient(#00d2ff, #3a7bd5); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .input-group { margin-bottom: 15px; }
        label { display: block; font-size: 12px; margin-bottom: 5px; color: #ccc; }
        input[type="text"], input[type="password"], textarea { width: 100%; padding: 12px; background: rgba(0, 0, 0, 0.2); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 8px; color: white; font-size: 14px; box-sizing: border-box; outline: none; font-family: monospace; }
        button { width: 100%; padding: 14px; background: linear-gradient(90deg, #00d2ff 0%, #3a7bd5 100%); border: none; border-radius: 8px; color: white; font-size: 16px; font-weight: 600; cursor: pointer; transition: 0.2s; margin-top: 10px; }
        button:hover { transform: translateY(-2px); box-shadow: 0 5px 15px rgba(0, 210, 255, 0.4); }
        .btn-secondary { background: rgba(255,255,255,0.1); font-size: 14px; padding: 10px; margin-top: 0;}
        #result-box { margin-top: 25px; padding: 20px; background: rgba(0, 0, 0, 0.3); border-radius: 8px; font-size: 14px; line-height: 1.6; display: none; }
        .loader { display: none; text-align: center; margin-top: 20px; color: #00d2ff; }
        
        /* Settings Modal */
        .settings-btn { position: absolute; top: 20px; right: 20px; background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.2); border-radius: 50%; width: 40px; height: 40px; cursor: pointer; display: flex; justify-content: center; align-items: center; font-size: 20px; transition: 0.3s; }
        .settings-btn:hover { background: rgba(255,255,255,0.2); transform: rotate(45deg); }
        #settings-modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); z-index: 1000; justify-content: center; align-items: center; }
        .modal-content { background: #1e2a35; padding: 30px; border-radius: 15px; width: 400px; border: 1px solid rgba(255,255,255,0.1); box-shadow: 0 10px 30px rgba(0,0,0,0.5); }
        .modal-close { float: right; cursor: pointer; font-size: 20px; color: #888; }
        .status-badge { display: inline-block; padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; margin-bottom: 10px; }
        .status-red { background: rgba(255,0,0,0.2); color: #ff4d4d; border: 1px solid #ff4d4d; }
        .status-green { background: rgba(0,255,0,0.2); color: #2ecc71; border: 1px solid #2ecc71; }
    </style>
</head>
<body>

<div class="settings-btn" onclick="openSettings()">⚙️</div>

<!-- Settings Modal -->
<div id="settings-modal">
    <div class="modal-content">
        <span class="modal-close" onclick="closeSettings()">&times;</span>
        <h3 style="margin-top:0; color:#00d2ff;">환경 설정</h3>
        
        <h4 style="margin-bottom:10px; border-bottom:1px solid #333; padding-bottom:5px;">🤖 AI 엔진 선택</h4>
        <div style="margin-bottom: 15px; display: flex; gap: 15px; font-size: 13px;">
            <label><input type="radio" name="ai-engine-type" value="gemini" id="engine-gemini" checked onchange="toggleEngineFields()"> ☁️ Google Gemini</label>
            <label><input type="radio" name="ai-engine-type" value="gauss" id="engine-gauss" onchange="toggleEngineFields()"> 🏢 사내 Gauss AI</label>
        </div>
        
        <div id="cfg-gemini-group">
            <div id="ai-status" class="status-badge status-red">🔴 연결되지 않음</div>
            <div class="input-group">
                <input type="password" id="cfg-ai-key" placeholder="AI API Key 입력 (Gemini Key)">
                <button class="btn-secondary" onclick="testAIConnection()" style="margin-top:5px; background:rgba(58, 123, 213, 0.5); font-weight:bold;">🔍 API 연결 및 지원 모델 불러오기</button>
            </div>
            <div class="input-group" style="margin-top:10px;">
                <label>API Key 지원 모델 선택</label>
                <select id="cfg-ai-model" onchange="saveSettings()" style="width: 100%; padding: 10px; background: rgba(0, 0, 0, 0.2); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 8px; color: white; margin-bottom: 10px; outline:none;">
                    <option value="">-- 위 버튼을 눌러 모델을 불러오세요 --</option>
                </select>
            </div>
        </div>
        
        <div id="cfg-gauss-group" style="display: none; background: rgba(0,0,0,0.2); padding: 15px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.1); margin-bottom: 15px;">
            <div class="input-group">
                <label>Gauss Base URL (OpenAI 호환)</label>
                <input type="text" id="cfg-gauss-endpoint" placeholder="예: https://api.samsung.net/v1">
            </div>
            <div class="input-group">
                <label>API Key (인증 토큰)</label>
                <input type="password" id="cfg-gauss-key" placeholder="발급받은 Key 입력">
            </div>
            <div class="input-group">
                <label>Model Name (선택적)</label>
                <input type="text" id="cfg-gauss-model" placeholder="예: gauss-language-v1">
            </div>
            <p style="font-size:11px; color:#888; margin:0;">* Langchain의 ChatOpenAI와 100% 동일한 규격으로 통신합니다.</p>
        </div>

        <h4 style="margin-bottom:10px; border-bottom:1px solid #333; padding-bottom:5px;">🔐 Gerrit 인증 방식 (선택)</h4>
        <div style="margin-bottom: 10px; display: flex; gap: 15px; font-size: 13px;">
            <label><input type="radio" name="gerrit-auth" value="ssh" id="auth-ssh" checked onchange="toggleAuthFields()"> 로컬 SSH Key 사용 (추천)</label>
            <label><input type="radio" name="gerrit-auth" value="http" id="auth-http" onchange="toggleAuthFields()"> HTTP 비밀번호 사용</label>
        </div>
        <div class="input-group">
            <label>Gerrit Username (SSH 및 HTTP 공통)</label>
            <input type="text" id="cfg-gerrit-user" placeholder="예: sunghyun.p">
        </div>
        <div class="input-group" id="cfg-gerrit-pass-group" style="display: none;">
            <label>HTTP Password (SSH 사용 시 불필요)</label>
            <input type="password" id="cfg-gerrit-pass" placeholder="Gerrit 비밀번호">
        </div>
        
        <button onclick="saveSettings()">설정 닫기</button>
    </div>
</div>

<div class="glass-panel">
    <h1>DALi AI Impact Analyzer</h1>
    <div id="section-pr">
        <div class="input-group">
            <label>GitHub 또는 Gerrit PR 주소 (줄바꿈으로 여러 개 입력 가능)</label>
            <textarea id="pr-links" rows="4" placeholder="실제 존재하는 PR 링크를 입력해주세요."></textarea>
        </div>
    </div>

    <button id="analyze-btn" onclick="startAnalysis()">🚀 커밋 파싱 및 분석 시작</button>
    <div class="loader" id="loader">🔄 코드를 분석하고 있습니다...</div>
    
    <div id="toggle-bar" style="margin-top: 20px; display: none; background: rgba(0,0,0,0.3); padding: 10px 15px; border-radius: 8px;">
        <label style="cursor: pointer; display: inline-flex; align-items: center;"><input type="checkbox" id="toggle-impact" checked onchange="toggleSections()" style="margin-right: 5px;"> 🎯 샘플 추천 보기</label>
        <label style="cursor: pointer; display: inline-flex; align-items: center; margin-left: 20px;"><input type="checkbox" id="toggle-review" checked onchange="toggleSections()" style="margin-right: 5px;"> 📝 라인별 코드 리뷰 보기</label>
    </div>
    <div id="result-box"></div>
</div>

<script>
    // --- 설정 모달 로직 ---
    function toggleEngineFields() {
        const isGauss = document.getElementById('engine-gauss').checked;
        document.getElementById('cfg-gauss-group').style.display = isGauss ? 'block' : 'none';
        document.getElementById('cfg-gemini-group').style.display = isGauss ? 'none' : 'block';
    }

    function toggleAuthFields() {
        const isHttp = document.getElementById('auth-http').checked;
        document.getElementById('cfg-gerrit-pass-group').style.display = isHttp ? 'block' : 'none';
    }

    function openSettings() { document.getElementById('settings-modal').style.display = 'flex'; toggleAuthFields(); toggleEngineFields(); }
    function closeSettings() { document.getElementById('settings-modal').style.display = 'none'; }
    
    function loadSettings() {
        document.getElementById('cfg-ai-key').value = localStorage.getItem('ai_key') || '';
        document.getElementById('cfg-gerrit-user').value = localStorage.getItem('gerrit_user') || '';
        document.getElementById('cfg-gerrit-pass').value = localStorage.getItem('gerrit_pass') || '';
        document.getElementById('cfg-gauss-endpoint').value = localStorage.getItem('gauss_endpoint') || '';
        document.getElementById('cfg-gauss-key').value = localStorage.getItem('gauss_key') || '';
        document.getElementById('cfg-gauss-model').value = localStorage.getItem('gauss_model') || '';
        
        const engineType = localStorage.getItem('ai_engine_type') || 'gemini';
        document.getElementById(engineType === 'gauss' ? 'engine-gauss' : 'engine-gemini').checked = true;
        toggleEngineFields();
        
        const savedModel = localStorage.getItem('ai_model');
        if (savedModel) {
            const select = document.getElementById('cfg-ai-model');
            select.innerHTML = `<option value="${savedModel}" selected>${savedModel} (저장됨)</option>`;
        }
        
        const authMode = localStorage.getItem('gerrit_auth_mode') || 'ssh';
        document.getElementById(authMode === 'ssh' ? 'auth-ssh' : 'auth-http').checked = true;
        toggleAuthFields();
        
        updateAIStatusUI(localStorage.getItem('ai_status_msg'), localStorage.getItem('ai_status_color'));
    }
    
    function saveSettings() {
        localStorage.setItem('ai_key', document.getElementById('cfg-ai-key').value);
        localStorage.setItem('ai_model', document.getElementById('cfg-ai-model').value);
        localStorage.setItem('gerrit_user', document.getElementById('cfg-gerrit-user').value);
        localStorage.setItem('gerrit_pass', document.getElementById('cfg-gerrit-pass').value);
        localStorage.setItem('gerrit_auth_mode', document.getElementById('auth-ssh').checked ? 'ssh' : 'http');
        
        localStorage.setItem('ai_engine_type', document.getElementById('engine-gauss').checked ? 'gauss' : 'gemini');
        localStorage.setItem('gauss_endpoint', document.getElementById('cfg-gauss-endpoint').value);
        localStorage.setItem('gauss_key', document.getElementById('cfg-gauss-key').value);
        localStorage.setItem('gauss_model', document.getElementById('cfg-gauss-model').value);
        closeSettings();
    }
    
    function updateAIStatusUI(msg, colorClass) {
        const badge = document.getElementById('ai-status');
        if(msg) {
            badge.className = "status-badge " + colorClass;
            badge.innerText = msg;
        }
    }

    async function testAIConnection() {
        const key = document.getElementById('cfg-ai-key').value;
        if(!key) {
            updateAIStatusUI("🔴 연결되지 않음 (키 없음)", "status-red");
            return;
        }
        
        updateAIStatusUI("🔄 구글 서버에서 모델 목록 불러오는 중...", "status-red");
        try {
            const response = await fetch('/api/test_ai', { 
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ai_key: key })
            });
            const data = await response.json();
            if(data.success && data.models.length > 0) {
                updateAIStatusUI(`🟢 연결 성공! (${data.models.length}개 모델 로드됨)`, "status-green");
                localStorage.setItem('ai_status_msg', `🟢 연결됨`);
                localStorage.setItem('ai_status_color', "status-green");
                
                const select = document.getElementById('cfg-ai-model');
                select.innerHTML = '';
                data.models.forEach(m => {
                    const opt = document.createElement('option');
                    opt.value = m.id;
                    opt.textContent = m.name;
                    select.appendChild(opt);
                });
                
                const savedModel = localStorage.getItem('ai_model');
                if(savedModel && Array.from(select.options).some(o => o.value === savedModel)) {
                    select.value = savedModel;
                }
            } else {
                updateAIStatusUI("🔴 연결 실패: 유효하지 않은 Key", "status-red");
            }
        } catch(e) {
            updateAIStatusUI("🔴 통신 오류 발생", "status-red");
        }
        saveSettings();
    }

    // --- 메인 로직 ---
    window.onload = loadSettings;

    function toggleSections() {
        const showImpact = document.getElementById('toggle-impact').checked;
        const showReview = document.getElementById('toggle-review').checked;
        document.querySelectorAll('.ai-impact-section').forEach(el => el.style.display = showImpact ? 'block' : 'none');
        document.querySelectorAll('.ai-review-section').forEach(el => el.style.display = showReview ? 'block' : 'none');
    }

    async function startAnalysis() {
        const urls = document.getElementById('pr-links').value.split('\\n').filter(u => u.trim() !== '');
        if(urls.length === 0) return alert("PR 링크를 입력해주세요.");
        
        const loader = document.getElementById('loader');
        const resultBox = document.getElementById('result-box');
        
        loader.style.display = 'block';
        resultBox.style.display = 'none';

        try {
            const response = await fetch('/api/parse_pr', { 
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    urls: urls,
                    ai_engine_type: localStorage.getItem('ai_engine_type') || 'gemini',
                    gauss_endpoint: localStorage.getItem('gauss_endpoint') || '',
                    gauss_key: localStorage.getItem('gauss_key') || '',
                    gauss_model: localStorage.getItem('gauss_model') || '',
                    ai_key: localStorage.getItem('ai_key'),
                    ai_model: localStorage.getItem('ai_model') || '',
                    gerrit_user: localStorage.getItem('gerrit_user'),
                    gerrit_token: localStorage.getItem('gerrit_pass'),
                    gerrit_auth_mode: localStorage.getItem('gerrit_auth_mode') || 'ssh'
                })
            });
            const results = await response.json();
            
            loader.style.display = 'none';
            resultBox.style.display = 'block';
            let html = '';
            
            for (const data of results) {
                if (data.error) {
                    html += `<p style="color:#ff4d4d; background:rgba(255,0,0,0.1); padding:15px; border-radius:8px;">❌ 오류: ${data.error}</p>`;
                    continue;
                }
                html += `
                <div style="background:rgba(255,255,255,0.05); padding:15px; border-radius:8px; margin-bottom:15px; border:1px solid rgba(255,255,255,0.1);">
                    <h4 style="margin:0 0 10px 0; color:#f39c12;">📁 ${data.repo}</h4>
                    <p style="margin:5px 0;"><b>제목:</b> ${data.title} <span style="color:#888; font-size:12px;">(by ${data.author})</span></p>
                    <p style="margin:5px 0;"><b>Before:</b> <span style="color:#ff4d4d; font-family:monospace;">${data.before_commit}</span></p>
                    <p style="margin:5px 0;"><b>After:</b> <span style="color:#2ecc71; font-family:monospace;">${data.after_commit}</span></p>
                    <hr style="border:0; border-top:1px solid rgba(255,255,255,0.1); margin: 15px 0;">
                    ${data.ai_analysis}
                </div>`;
            }
            
            resultBox.innerHTML = html;
            document.getElementById('toggle-bar').style.display = 'block';
        } catch (e) {
            loader.style.display = 'none';
            alert("서버 연결 오류가 발생했습니다.");
        }
    }
</script>
</body>
</html>
"""

class RequestHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML_CONTENT.encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        body = json.loads(post_data.decode('utf-8'))

        if self.path == '/api/test_ai':
            ai_key = body.get('ai_key', '')
            try:
                # Get available models for this API Key
                url = f"https://generativelanguage.googleapis.com/v1beta/models?key={ai_key}"
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                    models = []
                    for m in data.get('models', []):
                        if 'generateContent' in m.get('supportedGenerationMethods', []):
                            model_id = m['name'].replace('models/', '')
                            if 'gemini' in model_id:
                                models.append({"id": model_id, "name": f"{m.get('displayName', model_id)} ({model_id})"})
                    
                    self.send_response(200)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": True, "models": models}).encode('utf-8'))
            except Exception as e:
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode('utf-8'))

        elif self.path == '/api/parse_pr':
            urls = body.get('urls', [])
            ai_engine = body.get('ai_engine_type', 'gemini')
            gauss_endpoint = body.get('gauss_endpoint', '')
            gauss_key = body.get('gauss_key', '')
            gauss_model = body.get('gauss_model', '')
            ai_key = body.get('ai_key', '')
            ai_model = body.get('ai_model', '')
            g_user = body.get('gerrit_user', '')
            g_token = body.get('gerrit_token', '')
            g_mode = body.get('gerrit_auth_mode', 'ssh')
            results = []
            
            for url in urls:
                url = url.strip()
                if not url: continue
                
                meta = None
                if "github.com" in url:
                    meta = parse_github_pr(url)
                elif "gerrit" in url or "review" in url:
                    if g_mode == 'ssh':
                        meta = parse_gerrit_pr_ssh(url, g_user)
                    else:
                        meta = parse_gerrit_pr(url, g_user, g_token)
                else:
                    meta = {"error": f"지원하지 않는 URL 형식입니다: {url}"}
                    
                if meta and meta.get('success'):
                    diff_data = []
                    if "github.com" in url:
                        diff_data = fetch_github_diff(meta['owner'], meta['repo'], meta['before_commit'], meta['after_commit'])
                    elif meta.get('auth_mode') == 'ssh':
                        diff_data = fetch_gerrit_diff_ssh(meta['ssh_target'], meta['project'], meta['ref'])
                    else:
                        diff_data = fetch_gerrit_diff(meta['base_url'], meta['change_id'], g_user, g_token)
                    
                    analysis_html = ai_engine_inst.analyze(meta, diff_data, ai_key, ai_model, ai_engine, gauss_endpoint, gauss_key, gauss_model)
                    meta['ai_analysis'] = analysis_html
                    
                results.append(meta)
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(results).encode('utf-8'))

ai_engine_inst = RealGeminiAnalyzer()

def run_server():
    with socketserver.TCPServer(("", PORT), RequestHandler) as httpd:
        print(f"✅ Web App Server is running at http://localhost:{PORT}")
        httpd.serve_forever()

if __name__ == '__main__':
    run_server()
