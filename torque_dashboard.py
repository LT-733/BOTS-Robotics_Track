"""Read-only BBOS arm torque dashboard on port 5000."""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ENV_FILE = Path("/home/bracketbot/bbos/bbos/daemons/arm_left/.devenv/bbos-env.json")
PYTHON = Path("/home/bracketbot/bbos/bbos/daemons/arm_left/.venv/bin/python")
if os.environ.get("TORQUE_DASH_ENV") != "1":
    env = os.environ.copy()
    raw = json.loads(ENV_FILE.read_text())
    values = raw.get("env", raw)
    for key in ("PATH", "LD_LIBRARY_PATH", "PYTHONPATH", "NIX_PYTHONPATH"):
        if isinstance(values.get(key), str):
            env[key] = values[key]
    env["TORQUE_DASH_ENV"] = "1"
    os.execve(str(PYTHON), [str(PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]], env)

import numpy as np
from bbos import Config, Reader

LOCK = threading.Lock()
LATEST = {"ready": False, "message": "Waiting for arm feedback"}

HTML = r'''<!doctype html><meta charset="utf-8"><title>BracketBot Live Torque</title>
<style>
body{font:15px system-ui;background:#0b1020;color:#e8edf7;margin:24px}h1{margin:0 0 5px}
.muted{color:#94a3b8}.cards{display:flex;gap:16px;margin:18px 0}.card{background:#151d31;padding:16px;border-radius:12px;min-width:230px}
.value{font-size:32px;font-weight:700}.left{color:#38bdf8}.right{color:#fb7185}canvas{background:#11182a;border-radius:12px;width:100%;height:380px}
table{width:100%;border-collapse:collapse;margin-top:18px}th,td{text-align:right;padding:7px;border-bottom:1px solid #263249}th:first-child,td:first-child{text-align:left}
</style><h1>BracketBot live torque</h1><div class="muted">Read-only · joints J1–J6 · torque = motor current × motor Kt</div>
<div class="cards"><div class="card"><div class="left">LEFT</div><div id="lv" class="value">--</div><div id="lj" class="muted">waiting</div></div>
<div class="card"><div class="right">RIGHT</div><div id="rv" class="value">--</div><div id="rj" class="muted">waiting</div></div>
<div class="card"><div>Elapsed</div><div id="tm" class="value">--</div><div id="mode" class="muted">2-second baseline first</div></div></div>
<canvas id="plot" width="1200" height="380"></canvas><div id="tables"></div>
<script>
const hist=[], cv=document.getElementById('plot'),cx=cv.getContext('2d');
function draw(){cx.clearRect(0,0,cv.width,cv.height);cx.strokeStyle='#334155';cx.beginPath();for(let y=0;y<=4;y++){let py=20+y*82;cx.moveTo(55,py);cx.lineTo(1180,py)}cx.stroke();
cx.fillStyle='#94a3b8';cx.font='14px system-ui';for(let y=0;y<=4;y++)cx.fillText((2-y*.5).toFixed(1)+' Nm',4,25+y*82);
for(const [key,color] of [['l','#38bdf8'],['r','#fb7185']]){cx.strokeStyle=color;cx.lineWidth=3;cx.beginPath();hist.forEach((p,i)=>{let x=55+i*1125/599,y=348-Math.min(2,p[key])*164;(i?cx.lineTo(x,y):cx.moveTo(x,y))});cx.stroke()}}
function table(d){let h='<table><tr><th>Joint</th><th>Left torque</th><th>Left increase</th><th>Right torque</th><th>Right increase</th></tr>';
for(let j=1;j<=6;j++)h+=`<tr><td>J${j}</td><td>${d.left.torque_nm[j].toFixed(4)} Nm</td><td>${d.left.delta_nm[j].toFixed(4)} Nm</td><td>${d.right.torque_nm[j].toFixed(4)} Nm</td><td>${d.right.delta_nm[j].toFixed(4)} Nm</td></tr>`;return h+'</table>'}
async function tick(){try{let d=await(await fetch('/data',{cache:'no-store'})).json();if(!d.ready)return;lv.textContent=d.left.peak_delta_nm.toFixed(3)+' Nm';rv.textContent=d.right.peak_delta_nm.toFixed(3)+' Nm';
lj.textContent='peak J'+d.left.peak_joint; rj.textContent='peak J'+d.right.peak_joint;tm.textContent=d.time_s.toFixed(1)+'s';mode.textContent=d.calibrating?'MEASURING BASELINE':'LIVE';
hist.push({l:d.left.peak_delta_nm,r:d.right.peak_delta_nm});if(hist.length>600)hist.shift();draw();tables.innerHTML=table(d)}catch(e){}setTimeout(tick,100)}tick();
</script>'''

def sampler():
    global LATEST
    cfgs = [Config("arm_left"), Config("arm_right")]
    samples = [[], []]
    baseline = [np.zeros(8), np.zeros(8)]
    started = time.monotonic()
    with Reader("arm_left.state", keeptime=False) as left, Reader("arm_right.state", keeptime=False) as right:
        while True:
            now = time.monotonic()
            if not (left.ready() and right.ready()):
                time.sleep(.02); continue
            result = {"ready": True, "time_s": now-started, "calibrating": now-started < 2.0}
            for index, (name, reader, cfg) in enumerate(zip(("left","right"),(left,right),cfgs)):
                current = np.array(reader.data["current"], dtype=float)
                torque = current*np.asarray(cfg.kt, dtype=float)
                if now-started < 2.0:
                    samples[index].append(torque)
                    baseline[index] = np.median(np.stack(samples[index]), axis=0)
                delta = np.abs(torque-baseline[index])
                peak = int(np.argmax(delta[1:7]))+1
                result[name] = {"current_a":current.tolist(),"torque_nm":torque.tolist(),"delta_nm":delta.tolist(),
                                "peak_joint":peak,"peak_delta_nm":float(delta[peak])}
            with LOCK: LATEST = result
            time.sleep(.02)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/data":
            with LOCK: body=json.dumps(LATEST).encode()
            kind="application/json"
        elif self.path in ("/", "/index.html"):
            body=HTML.encode(); kind="text/html; charset=utf-8"
        else:
            self.send_error(404); return
        self.send_response(200); self.send_header("Content-Type",kind);self.send_header("Content-Length",str(len(body)));self.send_header("Cache-Control","no-store");self.end_headers();self.wfile.write(body)
    def log_message(self, *_): pass

if __name__ == "__main__":
    threading.Thread(target=sampler, daemon=True).start()
    print("Torque dashboard: http://0.0.0.0:5000", flush=True)
    ThreadingHTTPServer(("0.0.0.0",5000),Handler).serve_forever()
