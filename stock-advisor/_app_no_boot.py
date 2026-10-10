import sys, os, time
os.chdir(r'D:\correct_your_life\stock-advisor')
sys.path.insert(0, r'D:\correct_your_life\stock-advisor')
sys.path = [p for p in sys.path if p != r'C:\Users\Strong\AppData\Local\Temp\opencode']
os.environ['NO_PROXY'] = '*'
os.environ['no_proxy'] = '*'
sys.sa_bootstrap_done = True
try:
    import app
except Exception as e:
    print('import app error:', e, flush=True)
    raise
print('app import (no bootstrap) done', flush=True)
print('DB_CONF:', {k: ('***' if k == 'password' else v) for k, v in app.DB_CONF.items()}, flush=True)
t = time.time()
try:
    conn = app.get_conn()
    print('get_conn ok', time.time() - t, flush=True)
    conn.close()
except Exception as e:
    print('get_conn fail:', type(e).__name__, e, flush=True)
