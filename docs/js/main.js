const sidebar = document.getElementById('dynamic-tabs');
const contentBlocks = document.getElementById('content-blocks');
const searchInput = document.getElementById('search');
const noResults = document.getElementById('noResults');
const allTabBtn = document.querySelector('[data-target="all"]');

function renderUI() {
    DB_DATA.forEach(category => {
        // Build Sidebar Tab
        const btn = document.createElement('button');
        btn.className = 'tab-btn';
        btn.setAttribute('data-target', category.id);
        btn.innerHTML = `<span class="icon">${category.emoji}</span> ${category.category}`;
        sidebar.appendChild(btn);

        // Build Content Section
        const section = document.createElement('div');
        section.className = 'section active'; // All active by default
        section.id = `sec-${category.id}`;
        
        let html = `<div class="section-header"><h2>${category.emoji} ${category.category}</h2></div>`;
        
        // Passive Features
        if(category.features && category.features.length > 0) {
            html += `<div class="feature-grid">`;
            category.features.forEach(f => {
                html += `
                <div class="feature-card searchable">
                    <span class="feature-tag">Passive System</span>
                    <h3>${f.name}</h3>
                    <p>${f.desc}</p>
                </div>`;
            });
            html += `</div>`;
        }
        
        // Slash Commands
        if(category.commands && category.commands.length > 0) {
            html += `<div class="cmd-grid">`;
            category.commands.forEach(cmd => {
                const tagClass = cmd.access === 'admin' ? 'tag-admin' : (cmd.access === 'booster' ? 'tag-booster' : 'tag-general');
                html += `<div class="cmd-card searchable">
                    <div class="cmd-top">
                        <div class="cmd-syntax">${cmd.syntax}</div>
                        <div class="cmd-tag ${tagClass}">${cmd.access}</div>
                    </div>
                    <div class="cmd-desc">${cmd.desc}</div>`;
                    
                if (cmd.params && cmd.params.length > 0) {
                    html += `<span class="cmd-params-title">Parameters & Options</span><div class="cmd-params">`;
                    cmd.params.forEach(p => {
                        const reqClass = p.required ? 'req-true' : 'req-false';
                        const reqText = p.required ? 'Required' : 'Optional';
                        html += `
                        <div class="param-row">
                            <span class="param-name">${p.name}</span>
                            <span class="param-req ${reqClass}">${reqText}</span>
                            <span class="param-type">${p.type}</span>
                        </div>`;
                    });
                    html += `</div>`;
                }
                html += `</div>`;
            });
            html += `</div>`;
        }
        
        section.innerHTML = html;
        contentBlocks.appendChild(section);
    });
}

function handleTabSwitch(e) {
    const targetBtn = e.target.closest('button');
    if(!targetBtn) return;
    
    // Reset search when switching tabs
    searchInput.value = '';
    
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    targetBtn.classList.add('active');
    
    const targetId = targetBtn.getAttribute('data-target');
    const sections = document.querySelectorAll('.section');
    
    sections.forEach(sec => {
        // Reset Search Visibilities
        sec.querySelectorAll('.searchable').forEach(el => el.style.display = '');
        
        if(targetId === 'all') {
            sec.classList.add('active');
        } else {
            if(sec.id === `sec-${targetId}`) sec.classList.add('active');
            else sec.classList.remove('active');
        }
    });
    noResults.style.display = 'none';
}

function handleSearch(e) {
    const term = e.target.value.toLowerCase();
    
    // Switch to ALL tab automatically if typing, to search everything globally
    if(term.length > 0 && !allTabBtn.classList.contains('active')) {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        allTabBtn.classList.add('active');
        document.querySelectorAll('.section').forEach(sec => sec.classList.add('active'));
    }
    
    let anyVisible = false;
    
    document.querySelectorAll('.section').forEach(sec => {
        let secHasVisible = false;
        sec.querySelectorAll('.searchable').forEach(item => {
            if(item.textContent.toLowerCase().includes(term)) {
                item.style.display = '';
                secHasVisible = true;
                anyVisible = true;
            } else {
                item.style.display = 'none';
            }
        });
        
        // Hide entire section header if no content matches inside it
        if(secHasVisible) sec.style.display = 'block';
        else sec.style.display = 'none';
    });
    
    if(!anyVisible && term.length > 0) {
        noResults.style.display = 'block';
    } else {
        noResults.style.display = 'none';
    }
}

// Init
renderUI();
document.getElementById('sidebar').addEventListener('click', handleTabSwitch);
searchInput.addEventListener('input', handleSearch);