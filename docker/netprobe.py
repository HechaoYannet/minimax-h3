import json, re, socket, time, urllib.request
socket.setdefaulttimeout(30)
LIMIT = 20_000_000

def grab(url):
    with urllib.request.urlopen(url) as r:
        return r.read()

def measure(name, url, limit=LIMIT):
    req = urllib.request.Request(url, headers={"Range": f"bytes=0-{limit - 1}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req) as r:
            data = r.read(limit)
        dt = time.time() - t0
        print(f"{name:26s} {r.status} {len(data)/1e6:6.1f} MB  {dt:6.2f}s  {len(data)/1e6/dt:6.2f} MB/s")
    except Exception as e:
        print(f"{name:26s} ERR {type(e).__name__}: {str(e)[:60]}")

print("--- resolve real wheel urls ---")
torch_url = None
try:
    html = grab("https://download.pytorch.org/whl/test/cu132/torch/").decode()
    for href in re.findall(r'href="([^"]+torch-2\.14[^"]*cp314[^"]*manylinux[^"]*\.whl)"', html):
        torch_url = href if href.startswith("http") else "https://download.pytorch.org/whl/test/cu132/" + href.split("/")[-1]
        break
    print("torch:", (torch_url or "not found")[:110])
except Exception as e:
    print("torch index ERR", e)

numpy_url = None
try:
    j = json.loads(grab("https://pypi.org/pypi/numpy/json"))
    for f in j["urls"]:
        if f["filename"].endswith("manylinux_2_28_x86_64.whl"):
            numpy_url = f["url"]; break
    print("numpy:", (numpy_url or "not found")[:110])
except Exception as e:
    print("pypi json ERR", e)

print("--- throughput ---")
if torch_url: measure("download.pytorch.org", torch_url)
if numpy_url:
    measure("files.pythonhosted.org", numpy_url)
    measure("mirrors.aliyun pypi", numpy_url.replace("https://files.pythonhosted.org/packages", "https://mirrors.aliyun.com/pypi/packages"))
