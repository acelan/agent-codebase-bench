import re,sys
SIG=re.compile(r'^\s*(?:static\s+)?[A-Za-z_]\w*[\w\s\*&:]*?\s+(\w+)\s*\([^;]*$')
def run(path):
    lines=open('/workspace/linux/drivers/gpu/drm/i915/'+path).read().split('\n')
    depth=0; func=None; pend=None
    for i,l in enumerate(lines,1):
        s=l.strip()
        if s.startswith(('/','*','//')): continue
        if pend is None and not s.startswith(('}','else')) and re.match(r'^(?:static\s+)?[A-Za-z_]\w*',s) and SIG.search(l) and '(' in l and (l.split('//')[0].count('(')>0):
            m=SIG.search(l)
            if m: pend=m.group(1)
            else: pend=None
        # open brace activates pending func
        for ch in l:
            if ch=='{' and pend is not None:
                func=pend; pend=None
            if ch=='{': depth+=1
            if ch=='}':
                depth-=1
                # a function ends when depth returns to the level that started it; simplify
