document.addEventListener('DOMContentLoaded', () => {
    // ─── AUTHENTICATION GATE ───
    const session = JSON.parse(sessionStorage.getItem('questSession'));
    const FIFTEEN_MINUTES = 15 * 60 * 1000;
    
    if (!session || !session.passcode || !session.authenticatedAt || (Date.now() - session.authenticatedAt > FIFTEEN_MINUTES)) {
        // Unauthenticated or expired
        sessionStorage.removeItem('questSession');
        window.location.href = '/dashboard.html';
        return;
    }

    // Auth valid, fade in page
    document.body.style.opacity = '1';
    
    // Refresh the timestamp so interacting keeps the session alive
    session.authenticatedAt = Date.now();
    sessionStorage.setItem('questSession', JSON.stringify(session));

    const PASSCODE = session.passcode;

    // ─── CONSTANTS & STATE ───
    let questsData = [];
    let tierRewards = {};
    const API_URL = '/api/quests';

    const TIER_LABELS = {
        'common': '⭐ Common',
        'uncommon': '💎 Uncommon',
        'rare': '🌟 Rare'
    };

    const TASK_LABELS = {
        'message_count': 'Messages',
        'vc_minutes': 'VC Minutes',
        'reaction_count': 'Reactions'
    };

    // ─── DOM ELEMENTS ───
    const kpiTotal = document.getElementById('kpi-total');
    const kpiActive = document.getElementById('kpi-active');
    const kpiCommon = document.getElementById('kpi-common');
    const kpiUncommon = document.getElementById('kpi-uncommon');
    const kpiRare = document.getElementById('kpi-rare');
    const questsTbody = document.getElementById('quests-tbody');
    
    const questModal = document.getElementById('quest-modal');
    const modalTitle = document.getElementById('modal-title');
    const questForm = document.getElementById('quest-form');
    const formGroupActive = document.getElementById('form-group-active');
    const deleteModal = document.getElementById('delete-modal');

    // Logout
    document.getElementById('logout-btn').addEventListener('click', (e) => {
        e.preventDefault();
        sessionStorage.removeItem('questSession');
        window.location.href = '/dashboard.html';
    });

    // ─── API CLIENT ───
    async function apiRequest(method, body = null) {
        // Refresh token time on every action
        const sess = JSON.parse(sessionStorage.getItem('questSession'));
        sess.authenticatedAt = Date.now();
        sessionStorage.setItem('questSession', JSON.stringify(sess));

        const options = {
            method,
            headers: {
                'Authorization': `Bearer ${PASSCODE}`,
                'Content-Type': 'application/json'
            }
        };
        if (body) {
            options.body = JSON.stringify(body);
        }

        const response = await fetch(API_URL, options);
        if (response.status === 401 || response.status === 403) {
            sessionStorage.removeItem('questSession');
            window.location.href = '/dashboard.html';
            throw new Error("Unauthorized");
        }
        return await response.json();
    }

    // ─── RENDER DATA ───
    async function loadQuests() {
        try {
            document.querySelector('.status-dot').style.backgroundColor = '#f39c12';
            document.getElementById('connection-status').textContent = 'Fetching data...';

            const res = await apiRequest('GET');
            if (!res.success) throw new Error(res.error);

            questsData = res.data;
            if (res.tier_rewards) tierRewards = res.tier_rewards;

            renderTable();
            updateKPIs();

            document.querySelector('.status-dot').style.backgroundColor = '#2ecc71';
            document.getElementById('connection-status').textContent = 'Live Database Connection';
        } catch (e) {
            showToast(e.message, 'error');
            document.querySelector('.status-dot').style.backgroundColor = '#e74c3c';
            document.getElementById('connection-status').textContent = 'Connection Error';
            questsTbody.innerHTML = '<tr><td colspan="9" class="empty-state">Failed to load quests.</td></tr>';
        }
    }

    function updateKPIs() {
        let active = 0, common = 0, uncommon = 0, rare = 0;
        
        questsData.forEach(q => {
            if (q.is_active) active++;
            if (q.tier === 'common') common++;
            else if (q.tier === 'uncommon') uncommon++;
            else if (q.tier === 'rare') rare++;
        });

        kpiTotal.textContent = questsData.length;
        kpiActive.textContent = active;
        kpiCommon.textContent = common;
        kpiUncommon.textContent = uncommon;
        kpiRare.textContent = rare;
    }

    function renderTable() {
        if (questsData.length === 0) {
            questsTbody.innerHTML = '<tr><td colspan="9" class="empty-state">No quests found. Create one!</td></tr>';
            return;
        }

        questsTbody.innerHTML = '';
        questsData.forEach(q => {
            const tr = document.createElement('tr');
            
            const badgeClass = `badge badge-${q.tier}`;
            const reward = tierRewards[q.tier] || 0;
            const desc = q.description ? q.description : '<em>No description</em>';

            tr.innerHTML = `
                <td class="col-id">#${q.id}</td>
                <td class="col-name">${q.name}</td>
                <td class="col-desc" title="${q.description || ''}">${desc}</td>
                <td><span class="${badgeClass}">${TIER_LABELS[q.tier]}</span></td>
                <td>${TASK_LABELS[q.task_type] || q.task_type}</td>
                <td class="col-target">${q.target_goal}</td>
                <td class="col-reward">+${reward} XP</td>
                <td>
                    <label class="toggle-switch">
                        <input type="checkbox" class="quest-toggle" data-id="${q.id}" ${q.is_active ? 'checked' : ''}>
                        <span class="slider"></span>
                    </label>
                </td>
                <td class="actions">
                    <button class="btn-icon edit" data-id="${q.id}" title="Edit">✏️</button>
                    <button class="btn-icon delete" data-id="${q.id}" title="Delete">🗑️</button>
                </td>
            `;
            questsTbody.appendChild(tr);
        });

        // Attach listeners
        document.querySelectorAll('.quest-toggle').forEach(el => {
            el.addEventListener('change', handleToggleStatus);
        });
        document.querySelectorAll('.btn-icon.edit').forEach(el => {
            el.addEventListener('click', (e) => openQuestModal(parseInt(e.currentTarget.dataset.id)));
        });
        document.querySelectorAll('.btn-icon.delete').forEach(el => {
            el.addEventListener('click', (e) => openDeleteModal(parseInt(e.currentTarget.dataset.id)));
        });
    }

    // ─── TOGGLE STATUS ───
    async function handleToggleStatus(e) {
        const id = parseInt(e.target.dataset.id);
        const isActive = e.target.checked;
        
        try {
            const res = await apiRequest('PUT', { id, is_active: isActive });
            if (!res.success) throw new Error(res.error);
            
            // Update local state
            const q = questsData.find(q => q.id === id);
            if (q) q.is_active = isActive;
            
            updateKPIs();
            showToast(`Quest ${isActive ? 'activated' : 'deactivated'}`, 'success');
        } catch (err) {
            e.target.checked = !isActive; // Revert
            showToast("Failed to update status", 'error');
        }
    }

    // ─── CREATE / EDIT MODAL ───
    document.getElementById('btn-create-quest').addEventListener('click', () => openQuestModal(null));
    document.getElementById('modal-close-btn').addEventListener('click', closeQuestModal);
    document.getElementById('modal-cancel-btn').addEventListener('click', closeQuestModal);

    function openQuestModal(id) {
        if (id) {
            const q = questsData.find(q => q.id === id);
            modalTitle.textContent = 'Edit Quest';
            document.getElementById('quest-id').value = q.id;
            document.getElementById('quest-name').value = q.name;
            document.getElementById('quest-desc').value = q.description || '';
            document.getElementById('quest-tier').value = q.tier;
            document.getElementById('quest-task').value = q.task_type;
            document.getElementById('quest-target').value = q.target_goal;
            document.getElementById('quest-active').checked = q.is_active;
            formGroupActive.style.display = 'block'; // Show status toggle on edit
        } else {
            modalTitle.textContent = 'Create New Quest';
            questForm.reset();
            document.getElementById('quest-id').value = '';
            formGroupActive.style.display = 'none'; // Hide status toggle on create (default true backend)
        }
        questModal.classList.remove('hidden');
    }

    function closeQuestModal() {
        questModal.classList.add('hidden');
    }

    questForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const btn = document.getElementById('modal-submit-btn');
        btn.disabled = true;
        btn.textContent = 'Saving...';

        const id = document.getElementById('quest-id').value;
        const payload = {
            name: document.getElementById('quest-name').value,
            description: document.getElementById('quest-desc').value,
            tier: document.getElementById('quest-tier').value,
            task_type: document.getElementById('quest-task').value,
            target_goal: parseInt(document.getElementById('quest-target').value)
        };

        if (id) {
            payload.id = parseInt(id);
            payload.is_active = document.getElementById('quest-active').checked;
        }

        try {
            const method = id ? 'PUT' : 'POST';
            const res = await apiRequest(method, payload);
            if (!res.success) throw new Error(res.error);
            
            showToast(`Quest ${id ? 'updated' : 'created'} successfully!`, 'success');
            closeQuestModal();
            loadQuests();
        } catch (err) {
            showToast(err.message, 'error');
        } finally {
            btn.disabled = false;
            btn.textContent = 'Save Quest';
        }
    });

    // ─── DELETE MODAL ───
    let deleteTargetId = null;
    const warningBox = document.getElementById('delete-warning-box');
    const warningCount = document.getElementById('delete-progress-count');

    document.getElementById('delete-cancel-btn').addEventListener('click', closeDeleteModal);

    async function openDeleteModal(id) {
        const q = questsData.find(q => q.id === id);
        deleteTargetId = id;
        document.getElementById('delete-quest-name').textContent = q.name;
        warningBox.classList.add('hidden');
        
        // Fetch progress count
        try {
            const res = await apiRequest('DELETE', { id, confirm: false });
            if (res.progress_count > 0) {
                warningCount.textContent = res.progress_count;
                warningBox.classList.remove('hidden');
            }
        } catch(e) {
            console.error("Could not fetch progress count", e);
        }

        deleteModal.classList.remove('hidden');
    }

    function closeDeleteModal() {
        deleteModal.classList.add('hidden');
        deleteTargetId = null;
    }

    document.getElementById('delete-confirm-btn').addEventListener('click', async (e) => {
        if (!deleteTargetId) return;
        const btn = e.target;
        btn.disabled = true;
        btn.textContent = 'Deleting...';

        try {
            const res = await apiRequest('DELETE', { id: deleteTargetId, confirm: true });
            if (!res.success) throw new Error(res.error);
            
            showToast('Quest deleted permanently.', 'success');
            closeDeleteModal();
            loadQuests();
        } catch(err) {
            showToast(err.message, 'error');
        } finally {
            btn.disabled = false;
            btn.textContent = 'Yes, Delete';
        }
    });

    // ─── TOAST NOTIFICATIONS ───
    function showToast(message, type = 'success') {
        const container = document.getElementById('toast-container');
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        
        const icon = type === 'success' ? '✅' : '⚠️';
        toast.innerHTML = `<span>${icon}</span> <span>${message}</span>`;
        
        container.appendChild(toast);
        
        setTimeout(() => {
            toast.classList.add('fade-out');
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    }

    // INIT
    loadQuests();
});
