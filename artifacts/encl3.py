import re,sys
KW={'if','for','while','switch','return','sizeof','else','do','case'}
init=re.compile(r'^\s*(?:static\s+)?(?:[\w\*&:\s]+?)\b([A-Za-z_]\w*)\s*\(', re.M)
def endsig(l):
    return False
def enclosing(path,targets):
    lines=open('/workspace/linux/drivers/gpu/drm/i915/'+path).read().split('\n')
    func=None; pend=None; depth=0
    out={}
    for i,l in enumerate(lines,1):
        s=l.strip()
        if s.startswith(('/','*','//')): 
            # still count braces? comments could contain braces; skip counting in comments
            # ignore
            if i in targets: out[i]=(func,depth)
            continue
        # detect pending signature start
        if pend is None:
            # line has '(' and ends with '{' OR may continue
            m=init.match(l)
            if m and m.group(1) not in KW and '(' in l:
                # ensure it looks like a function def (has ')' before '{' on this line and ends with '{')
                name=m.group(1)
                # last non-space char
                stripped=l.rstrip()
                if stripped.endswith('{') and ')' in stripped[:stripped.rfind('{')]:
                    pend=name
                # else could be multi-line signature; handle next line too
        # brace update; when we hit '{' and pend set -> commit
        for ch in l:
            if ch=='{':
                if pend is not None:
                    func=pend; pend=None
                depth+=1
            elif ch=='}':
                depth-=1
        if i in targets:
            out[i]=(func)
    return out
for e in sys.argv[1:]:
    f,t=e.rsplit(':',1)
    t=int(t)
    r=enclosing(f,[t])
    print(f"{f}:{t} -> {r.get(t)}")

# quick test helper usage