<script>
const form = document.getElementById('research-form');
const runBtn = document.getElementById('run-btn');
const progress = document.getElementById('progress');
const progressFill = document.getElementById('progress-fill');
const progressText = document.getElementById('progress-text');
const results = document.getElementById('results');
const stats = document.getElementById('stats');
const actionArea = document.getElementById('action-area');
const errorMsg = document.getElementById('error-msg');
const outputType = document.getElementById('output_type');
const sheetInput = document.getElementById('sheet-input-group');

outputType.addEventListener('change', () => {
    sheetInput.classList.toggle('show', outputType.value === 'sheets');
});

let currentTaskId = null;

form.addEventListener('submit', async (e) => {
    e.preventDefault();
    errorMsg.style.display = 'none';
    results.style.display = 'none';
    progress.style.display = 'block';
    progressFill.style.width = '0%';
    progressText.textContent = 'Starting research...';
    runBtn.disabled = true;

    const formData = new FormData(form);
    try {
        const resp = await fetch('/run', { method: 'POST', body: formData });
        if (!resp.ok) {
            const text = await resp.text();
            showError('Research request failed with HTTP ' + resp.status + '.');
            return;
        }
        if (!resp.headers.get('content-type') || !resp.headers.get('content-type').includes('application/json')) {
            const text = await resp.text();
            showError('Research request failed: unexpected response type.');
            return;
        }
        const data = await resp.json();
        if (data.error) { showError(data.error); return; }
        currentTaskId = data.task_id;
        pollStatus(currentTaskId);
    } catch (err) {
        showError('Failed to start research: ' + err.message);
    }
});

async function pollStatus(taskId) {
    const stages = [
        { check: (s) => s === 'searching', text: 'Searching for relevant pages...', pct: 15 },
        { check: (s) => s === 'scraping', text: 'Scraping page content...', pct: 40 },
        { check: (s) => s === 'analyzing', text: 'Extracting and analyzing keywords...', pct: 70 },
        { check: (s) => s === 'complete', text: 'Research complete!', pct: 100 },
    ];

    while (true) {
        const resp = await fetch('/status/' + taskId);
        const data = await resp.json();
        
        if (data.status === 'error') { showStatsError(data); return; }
        
        for (const stage of stages) {
            if (stage.check(data.status)) {
                progressText.textContent = stage.text;
                progressFill.style.width = stage.pct + '%';
                break;
            }
        }
        
        if (data.status === 'complete') {
            showResults(data);
            return;
        }
        
        await new Promise(r => setTimeout(r, 1500));
    }
}

function showResults(data) {
    progressFill.style.width = '100%';
    runBtn.disabled = false;
    results.style.display = 'block';
    
    var sourceText = data.data_source || 'Live Firecrawl Data';
    var versionText = data.version || 'local';
    var statsInfo = '<div style="text-align:center;margin-bottom:16px;font-size:13px;color:#888;">Data Source: <strong>' + sourceText + '</strong> | Version: <strong>' + versionText + '</strong>';
    if (data.search_stats) {
        var st = data.search_stats;
        statsInfo += '<br>Search: ' + st.raw_count + ' raw &rarr; ' + st.filtered_count + ' filtered &rarr; ' + st.scrape_success + ' scraped';
        if (st.scrape_fail > 0) statsInfo += ' (' + st.scrape_fail + ' failed)';
    }
    statsInfo += '</div>';
    
    stats.innerHTML = statsInfo + [
        { num: data.serp.length, label: 'SERP Pages' },
        { num: data.keywords.length, label: 'Keywords Extracted' },
        { num: data.intent_summary.length, label: 'Intent Types' },
        { num: data.clusters.length, label: 'Theme Clusters' },
    ].map(function(s) { return '<div class="stat-card"><div class="num">' + s.num + '</div><div class="label">' + s.label + '</div></div>'; }).join('');

    // Intent Summary detail
    let intentHtml = '<div class="intent-summary"><h3 style="font-size:15px;font-weight:600;color:#2F5496;margin-bottom:12px;">Intent Summary</h3><div class="stat-grid">';
    data.intent_summary.forEach(function(s) {
        intentHtml += '<div class="stat-card"><div class="num">' + s.estimated_share + '</div>' +
            '<div class="label" style="font-weight:600;color:#2F5496;margin-bottom:4px;">' + s.search_intent + '</div>' +
            '<div style="font-size:10px;color:#888;margin-top:2px;">Examples: ' + s.sample_keywords.substring(0, 60) + '...</div>' +
            '<div style="font-size:10px;color:#aaa;margin-top:2px;">Pages: ' + s.ranking_pages + '</div></div>';
    });
    intentHtml += '</div></div>';
    actionArea.insertAdjacentHTML('beforebegin', intentHtml);
    const outputType = document.getElementById('output_type').value;
    const sheetId = document.getElementById('sheet_id').value;
    const keyword = document.getElementById('keyword').value;
    
    let actions = '';
    if (outputType === 'excel') {
        actions = '<a href="/download/' + currentTaskId + '?type=xlsx" class="btn-success">📥 Download Excel (.xlsx)</a>';
        actions += ' <a href="/download/' + currentTaskId + '?type=csv" class="btn-outline" style="text-decoration:none;display:inline-block;">📄 Download CSV</a>';
    } else if (outputType === 'csv') {
        actions = '<a href="/download/' + currentTaskId + '?type=csv" class="btn-success">📄 Download CSV</a>';
    } else {
        actions = '<button onclick="exportToSheets()" class="btn-success" id="export-btn">📊 Export to Google Sheets</button>';
        if (sheetId) {
            actions += '<p style="margin-top:8px;font-size:13px;color:#666;">Sheet ID: ' + sheetId + '</p>';
        }
    }
    
    actionArea.innerHTML = '<div class="action-buttons">' + actions + '</div>';
}

// Google Sheet operations (global scope for onclick)
var _pickerLoaded = false;
function pickerCallback(data) {
    if (data.action == 'picked') {
        var doc = data.docs[0];
        fetch('/api/google/select-sheet', {method:'POST', headers:{'Content-Type':'application/json'},
            body:JSON.stringify({spreadsheetId:doc.id, spreadsheetName:doc.name, spreadsheetUrl:doc.url})})
        .then(function(r){return r.json();}).then(function(resp){
            if (resp.ok) {
                document.getElementById('sheet-selected').className='sheet-info';
                document.getElementById('sheet-name').textContent=resp.spreadsheetName;
                document.getElementById('sheet-url').href=resp.spreadsheetUrl;
            } else { showError(resp.error||'Select failed.'); }
        }).catch(function(){ showError('Unable to select sheet.'); });
    }
}
function loadPickerApi(callback) {
    if (_pickerLoaded) { if(callback)callback(); return; }
    var s = document.createElement('script');
    s.src = 'https://apis.google.com/js/api.js';
    s.onload = function(){ gapi.load('picker', {callback:function(){_pickerLoaded=true; if(callback)callback();}}); };
    document.body.appendChild(s);
}
function chooseGoogleSheet() {
    fetch('/api/google/picker-token', {method:'GET', credentials:'same-origin'})
    .then(function(r){return r.json();}).then(function(d){
        if (d.error) { showError(d.error); return; }
        loadPickerApi(function(){
            var view = new google.picker.View(google.picker.ViewId.SPREADSHEETS);
            new google.picker.PickerBuilder().addView(view).setOAuthToken(d.accessToken)
                .setDeveloperKey(d.pickerApiKey).setAppId(d.projectNumber)
                .setSelectableMimeTypes('application/vnd.google-apps.spreadsheet')
                .setCallback(pickerCallback).build().setVisible(true);
        });
    }).catch(function(){ showError('Unable to open sheet picker.'); });
}
function createNewSheet() {
    var btn = document.activeElement; btn && (btn.disabled=true, btn.textContent='Creating...');
    var kw = document.getElementById('keyword').value || 'research';
    fetch('/api/google/create-sheet', {method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json'}, body:JSON.stringify({keyword:kw})})
    .then(function(r){return r.json();}).then(function(d){
        btn && (btn.disabled=false, btn.textContent='Create New Sheet');
        if (d.ok) {
            document.getElementById('sheet-selected').className='sheet-info';
            document.getElementById('sheet-name').textContent=d.spreadsheetName;
            document.getElementById('sheet-url').href=d.spreadsheetUrl;
        } else { showError(d.error||'Create failed.'); }
    }).catch(function(){ showError('Unable to create sheet.'); btn&&(btn.disabled=false); });
}
function disconnectGoogle() {
    fetch('/auth/google/disconnect', {method:'POST', credentials:'same-origin'})
    .then(function(r){return r.json();}).then(function(d){
        if (d.ok) { location.reload(); }
        else { showError('Disconnect failed.'); }
    }).catch(function(){ showError('Disconnect failed.'); });
}

async function exportToSheets() {
    var btn = document.getElementById('export-btn');
    btn.disabled = true;
    btn.textContent = 'Exporting...';
    try {
        var resp = await fetch('/export-sheets/' + currentTaskId, { method: 'POST',
            headers: { 'Content-Type': 'application/json' }, body: '{}' });
        var data = await resp.json();
        if (data.error) { alert('Error: ' + data.error); btn.disabled=false; btn.textContent='Retry'; }
        else {
            actionArea.innerHTML = '<div style="text-align:center;"><p style="color:#2d7d46;font-weight:600;margin-bottom:12px;">✅ Exported to Google Sheets!</p>' +
                '<a href="' + data.url + '" target="_blank" class="btn-success">📊 Open Sheet</a></div>';
        }
    } catch(e) { alert('Export failed: ' + e.message); btn.disabled=false; btn.textContent='Retry'; }
}

function showError(msg) {
    errorMsg.textContent = msg;
    errorMsg.style.display = 'block';
    progress.style.display = 'none';
    runBtn.disabled = false;
}

function showStatsError(data) {
    var info = '<div style="font-size:11px;color:#999;margin-bottom:8px;">Data Source: ' + (data.data_source || 'Live Firecrawl Data') + ' | Version: ' + (data.version || 'local') + '</div>';
    info += data.message || 'An error occurred.';
    if (data.search_stats) {
        var st = data.search_stats;
        if (st.raw_count > 0) {
            info += '<br><br><strong>Search Statistics:</strong><br>';
            info += 'Raw results: ' + st.raw_count + '<br>';
            info += 'After dedup: ' + st.dedup_count + '<br>';
            info += 'After filter: ' + st.filtered_count + '<br>';
            info += 'Scraped: ' + st.scrape_success + '<br>';
            if (st.scrape_fail > 0) info += 'Failed: ' + st.scrape_fail;
            if (st.scrape_failures && st.scrape_failures.length > 0) {
                info += '<br><br><strong>Failure reasons:</strong><br>';
                st.scrape_failures.slice(0, 3).forEach(function(f) {
                    info += '&bull; ' + f.url + ': ' + f.reason + '<br>';
                });
            }
        } else {
            info += '<br><br><strong>Search Statistics:</strong><br>';
            info += 'Raw: ' + st.raw_count + ' | Filtered: ' + st.filtered_count;
            if (st.errors) info += '<br>Errors: ' + st.errors.join('<br>');
        }
    }
    errorMsg.innerHTML = info;
    errorMsg.style.display = 'block';
    progress.style.display = 'none';
    runBtn.disabled = false;
}
