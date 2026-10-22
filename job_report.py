"""Plain-text Telegram job cards and explainable, display-only match scores.

No model, external compensation estimates, network requests or ranking changes.
Unknown evidence earns no evidence points; this is not a hiring probability.
"""
import re

HEADER = ('Jobs · next batch\n/fit ID · /save ID · /applied ID · /jobscore ID\n'
          'Scores are estimates, not hiring odds. Reply to use list numbers.')
WEIGHTS = {'Entry level':30, 'Location':25, 'Pay evidence':20, 'Visa evidence':10,
           'Company estimate':10, 'Recency':5}
VISA = re.compile(r'\bvisa sponsorship\s+(?:is\s+)?(?:available|offered|provided)\b|\bsponsor(?:s|ship)?\s+(?:for\s+)?(?:h-?1b|visas?)\b', re.I)


def clean(value):
    return re.sub(r'\s+', ' ', re.sub(r'[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]', ' ', str(value or ''))).strip()


def money(value):
    def amount(number):
        return '$'+format(number / 1000, '.1f').rstrip('0').rstrip('.')+'k'
    return amount(value[0]) if value[0]==value[1] else amount(value[0])+'–'+amount(value[1])


def assess(job, cfg, now=None):
    import jobs
    import job_compensation
    import job_requirements
    text, title = job.get('text') or '', job.get('title') or ''
    details = bool(text.strip()) and job.get('details_checked') is not False
    years = jobs.required_years(text)
    junior = bool(re.search(r'\b(?:new grad(?:uate)?|entry[ -]level|junior|university|early career|campus|graduate)\b',title,re.I))
    comp = job_compensation.analyze(text, job.get('pay'), job.get('pay_basis')=='base', cfg.get('min_total_comp') or 170000)
    # Previously prepared compensation retains structured base evidence even
    # when a compact stored description no longer contains the salary paragraph.
    saved = job.get('compensation')
    if isinstance(saved,dict) and saved.get('target')==comp['target']:
        comp = saved
    base = comp.get('base')
    no_visa = bool(jobs.NO_SPONSOR.search(text) or re.search(r'\b(?:do not|does not|cannot|will not|unable to|not able to)\s+(?:provide|offer|sponsor|support)\b[^.\n]{0,50}\b(?:visas?|h-?1b|sponsorship)\b',text,re.I))
    visa = bool(VISA.search(text)) and not no_visa
    eligible = jobs.eligibility_flags(text)
    hints = job_requirements.flags(job)
    phd = any(flag.startswith('Degree: PhD explicitly required') for flag in hints)
    where = (job.get('location') or '').lower()
    verified_place = job.get('location_verified') is not False and bool(where) and where.strip()!='remote'
    points = {}
    points['Entry level'] = (0 if years is not None and years>=3 else 12 if years==2 else 20 if years==1
                             else 30 if junior and details else 24 if junior else 15)
    if jobs.has(where,cfg.get('home_locations',[])):
        points['Location']=25
    elif jobs.has(where,cfg.get('locations',[])):
        points['Location']=20
    elif jobs.location_ok(where,bool(job.get('remote')),cfg):
        points['Location']=16
    else:
        points['Location']=0
    if not verified_place:
        points['Location']=min(points['Location'],10)
    floor=cfg.get('min_pay') or 135000
    base_points = (8 if base[0]>=floor else 4 if base[1]>=floor else 0) if base else 0
    points['Pay evidence']=base_points+{'meets':12,'possible':6}.get(comp.get('status'),0)
    points['Visa evidence']=10 if visa and details else 0
    ratings=job.get('ratings') or {}
    numbers=[ratings.get(key) for key in ('pay','prestige','wlb','exits')]
    points['Company estimate']=round(sum(numbers)/2) if all(type(n) is int and 1<=n<=5 for n in numbers) else 0
    age=jobs.age_days(job,now)
    points['Recency']=0 if age is None else 5 if age<=7 else 4 if age<=14 else 2 if age<=30 else 1
    score=sum(points.values())
    checks = [details and (years is not None or junior), verified_place and bool(where),
              comp.get('status') in ('meets','possible','below'), details and (visa or no_visa), age is not None]
    warnings=[]
    caps=[]
    if no_visa:
        score=min(score,39)
        warnings.append('Posting says no sponsorship')
        caps.append('39/100 maximum: posting says no sponsorship')
    if phd:
        points['Entry level']=0
        score=min(sum(points.values()),49,score)
        caps.append('49/100 maximum: PhD explicitly required')
    view=dict(job,title=title,location=job.get('location',''),remote=bool(job.get('remote')))
    if (jobs.score_job(view,cfg,now) is None or jobs.senior_pay(view,cfg) or
        (cfg.get('max_years') and years is not None and years>=cfg['max_years']) or
        any(flag in jobs.INELIGIBLE for flag in eligible) or comp.get('status')=='below' or
        (cfg.get('min_pay') and base and base[1]<cfg['min_pay'])):
        score=0
        warnings.append('Outside current eligibility/filter rules')
        caps.append('0/100: outside current eligibility/filter rules')
    if not details:
        warnings.append('posting details not read; eligibility unverified')
    if not verified_place:
        warnings.append('Location unverified')
    for flag in hints:
        if flag.startswith(('pay:', 'visa:', 'posting details not read')):
            continue
        if flag in job_compensation.notes(comp) or flag in ('✅ mentions sponsorship','⚠️ says no sponsorship'):
            continue
        warnings.append(clean(flag).lstrip('⚠️🔎 ').strip())
    # Eligibility text must remain visible even on compact cards.
    warnings.extend(clean(flag).lstrip('⚠️🔎 ').strip() for flag in eligible)
    return {'score':max(0,min(100,score)), 'points':points,'evidence':sum(checks),'compensation':comp,
            'base':base,'visa':'Posting mentions visa sponsorship; verify' if visa and details else 'No sponsorship' if no_visa else 'Visa not stated',
            'age':age,'warnings':list(dict.fromkeys(warnings)), 'caps':caps,'details_checked':details}


def card(job,cfg,number,job_id=None,now=None):
    result=assess(job,cfg,now)
    identity=f' [{clean(job_id)}]' if job_id else ''
    company=clean(job.get('company',''))[:90]
    title=clean(job.get('title',''))[:180]
    place=clean(job.get('location',''))[:90] or 'Location unknown'
    if job.get('remote') and 'remote' not in place.lower():
        place+=' · Remote'
    age=result['age']
    date='Date unknown' if age is None else 'Posted today' if age==0 else f'Posted {age}d ago'
    comp=result['compensation']
    base='Base '+money(result['base']) if result['base'] else 'Base unknown'
    target=f"${comp['target']/1000:g}k"
    state=comp.get('status')
    total='Total unknown'
    if comp.get('total'):
        label='target possible' if state=='possible' else 'below target' if state=='below' else 'stated'
        total='Total '+money(comp['total'])+' ('+label+')'
    elif comp.get('subtotal') and state in ('meets','possible') and comp['subtotal'] == result['base']:
        # Only a base range is stated: say what it means for the target instead of repeating it.
        total = f'{target}+ total: yes (base alone)' if state=='meets' else f'{target} total: possible'
    elif comp.get('subtotal') and state in ('meets','possible'):
        label='target supported' if state=='meets' else 'target possible'
        total='Annual subtotal '+money(comp['subtotal'])+' ('+label+')'
    tier=f" · tier {job['tier']}" if job.get('tier') else ''
    visa=result['visa']
    sponsors=(job.get('ratings') or {}).get('sponsor')
    if visa=='Visa not stated' and sponsors is not None:
        # The company-level hint is an estimate; the posting itself said nothing.
        visa+=' (company usually sponsors; estimate)' if sponsors else " (company usually doesn't sponsor; estimate)"
    lines=[f'#{number}  {company}{tier}{identity}',title,
           f'📍 {place} · {date}',f"🎯 Match {result['score']}/100 · Evidence {result['evidence']}/5",
           f'💵 {base} · {total}',f"🛂 {visa}"]
    extras=[]
    if comp.get('bonus') or comp.get('equity'):
        extras.append('Bonus/equity values are not guaranteed')
    else:
        import jobs
        mentioned=[kind for kind in jobs.extra_pay(job.get('text','')) if not (kind=='sign-on' and comp.get('signon'))]
        if mentioned:
            extras.append('Additional '+', '.join(mentioned)+' mentioned; amounts unverified')
    if comp.get('signon'):
        extras.append('Sign-on '+money(comp['signon'])+' one-time; excluded from annual total')
    warnings=list(dict.fromkeys(result['warnings']+extras))
    for warning in warnings:
        if not result.get('details_checked', bool((job.get('text') or '').strip())) and warning=='Entry-level fit: unverified':
            continue
        concise=warning.replace('; check first-job eligibility','').replace('; verify your availability','').replace('; check eligibility','')
        lines.append('⚠️ '+concise)
    lines.append(job['url'])  # Complete URL stays last; callers budget whole cards.
    return '\n'.join(lines)


def explain(job,cfg,now=None):
    result=assess(job,cfg,now)
    lines=[clean(job.get('company'))[:90]+' · '+clean(job.get('title'))[:180],f"Match {result['score']}/100 · Evidence {result['evidence']}/5"]
    lines.extend(f"• {name}: {result['points'][name]}/{maximum}" for name,maximum in WEIGHTS.items())
    lines.extend('Cap: '+cap for cap in result['caps'])
    lines.extend('⚠️ '+warning for warning in result['warnings'])
    lines.append('Evidence covers experience, location, annual pay, visa and posting date. Unknowns earn no evidence points. No-sponsorship, PhD and eligibility warnings can cap the score. Company ratings are estimates. This is not hiring odds or a sponsorship promise. Display only; list order follows existing priority rules.')
    return '\n'.join(lines)


def coverage(counts):
    states = ('healthy','partial','failed','stale','never-checked')
    parts = [f"{counts.get(state,0)} {state}" for state in states if counts.get(state)]
    return 'Sources: ' + (' · '.join(parts) if parts else 'none checked') + ' · /sources'
