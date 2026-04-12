document.addEventListener('DOMContentLoaded', () => {
    const loader = document.getElementById('loader');
    
    // UI Elements
    const btn7d = document.getElementById('btn-7d');
    const btn30d = document.getElementById('btn-30d');
    const btnWeek = document.getElementById('btn-this-week');
    const btnMonth = document.getElementById('btn-this-month');
    const btnYear = document.getElementById('btn-this-year');
    const dateStartElem = document.getElementById('date-start');
    const dateEndElem = document.getElementById('date-end');
    
    const allBtns = [btn7d, btn30d, btnWeek, btnMonth, btnYear];

    let rawData = [];
    let charts = {};

    Chart.defaults.color = '#CCCCCC';
    Chart.defaults.font.family = 'Inter';

    // Utility: Format Date as YYYY-MM-DD
    function toStr(dateObj) {
        return dateObj.toISOString().split('T')[0];
    }
    
    // Set Active Button
    function setActive(target) {
        allBtns.forEach(b => b.classList.remove('active'));
        if(target) target.classList.add('active');
    }

    // Handle Time Windows
    function setDateBounds(preset) {
        const today = new Date();
        let startD = new Date();
        let endD = new Date();

        switch(preset) {
            case '7d':
                startD.setDate(today.getDate() - 7);
                break;
            case '30d':
                startD.setDate(today.getDate() - 30);
                break;
            case 'week':
                // ISO 8601 Week (Monday start)
                let day = today.getDay() || 7; 
                startD.setDate(today.getDate() - day + 1);
                break;
            case 'month':
                // This Month (1st to current)
                startD = new Date(today.getFullYear(), today.getMonth(), 1);
                break;
            case 'year':
                // This Year (Jan 1st to current)
                startD = new Date(today.getFullYear(), 0, 1);
                break;
        }

        dateStartElem.value = toStr(startD);
        dateEndElem.value = toStr(endD);
        renderDashboard();
    }

    // Event Listeners
    btn7d.addEventListener('click', () => { setActive(btn7d); setDateBounds('7d'); });
    btn30d.addEventListener('click', () => { setActive(btn30d); setDateBounds('30d'); });
    btnWeek.addEventListener('click', () => { setActive(btnWeek); setDateBounds('week'); });
    btnMonth.addEventListener('click', () => { setActive(btnMonth); setDateBounds('month'); });
    btnYear.addEventListener('click', () => { setActive(btnYear); setDateBounds('year'); });
    
    dateStartElem.addEventListener('change', () => { setActive(null); renderDashboard(); });
    dateEndElem.addEventListener('change', () => { setActive(null); renderDashboard(); });

    async function fetchStats() {
        try {
            loader.innerText = "Syncing secure connection...";
            loader.style.color = "var(--accent)";
            
            const response = await fetch('/api/stats');
            const resData = await response.json();
            
            if (resData.success && resData.data) {
                rawData = resData.data.sort((a, b) => new Date(a.date) - new Date(b.date));
                loader.innerText = "● Live synced";
                loader.style.color = "var(--green)";
                
                // Trigger default 7D view
                setDateBounds('7d');
                setActive(btn7d);
            } else {
                throw new Error("Invalid format");
            }
        } catch (err) {
            console.error(err);
            loader.innerText = "Failed to fetch secure API.";
            loader.style.color = "var(--red)";
        }
    }

    function renderDashboard() {
        const startStr = dateStartElem.value;
        const endStr = dateEndElem.value;

        // Slice data according to bounds
        const d = rawData.filter(row => {
            if (startStr && row.date < startStr) return false;
            if (endStr && row.date > endStr) return false;
            return true;
        });

        let sumMsgs = 0, sumVc = 0, sumJoins = 0, sumLeaves = 0, sumVerif = 0;
        let labels = [], joinsData = [], leavesData = [], netData = [], msgData = [], vcData = [];
        let channelAggr = {};

        for (let row of d) {
            sumMsgs += row.total_messages || 0;
            sumVc += row.total_voice_minutes || 0;
            sumJoins += row.new_joins || 0;
            sumLeaves += row.new_leaves || 0;
            
            labels.push(row.date);
            joinsData.push(row.new_joins || 0);
            leavesData.push(row.new_leaves || 0);
            netData.push((row.new_joins || 0) - (row.new_leaves || 0));
            msgData.push(row.total_messages || 0);
            vcData.push(row.total_voice_minutes || 0);

            if (row.granular_json) {
                sumVerif += row.granular_json.new_verifications || 0;
                if (row.granular_json.top_text_channels) {
                    row.granular_json.top_text_channels.forEach(ch => {
                        let chName = `#${ch.channel_id}`; 
                        channelAggr[chName] = (channelAggr[chName] || 0) + (ch.count || 0);
                    });
                }
            }
        }

        document.getElementById('stat-msgs').innerText = sumMsgs.toLocaleString();
        document.getElementById('stat-vc').innerText = sumVc.toLocaleString();
        document.getElementById('stat-growth').innerText = `${sumJoins > sumLeaves ? '+' : ''}${(sumJoins - sumLeaves).toLocaleString()}`;
        document.getElementById('stat-verifs').innerText = sumVerif.toLocaleString();

        drawGrowthChart(labels, joinsData, leavesData, netData);
        drawTrafficChart(labels, msgData, vcData);
        drawChannelChart(channelAggr);
        
        // Draw Ops using latest available day in range
        drawOps(d.length > 0 ? d[d.length - 1] : null);
    }

    function drawGrowthChart(labels, joins, leaves, net) {
        let ctx = document.getElementById('growthChart').getContext('2d');
        if (charts.growth) charts.growth.destroy();

        charts.growth = new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [
                    { label: 'Net Growth', data: net, borderColor: '#F2C21A', backgroundColor: 'rgba(242,194,26,0.1)', fill: true, tension: 0.3 },
                    { label: 'Joins', data: joins, borderColor: '#43b581', borderDash: [5, 5], tension: 0.3 },
                    { label: 'Leaves', data: leaves, borderColor: '#f04747', borderDash: [5, 5], tension: 0.3 }
                ]
            },
            options: { responsive: true, maintainAspectRatio: false }
        });
    }

    function drawTrafficChart(labels, msgs, vc) {
        let ctx = document.getElementById('trafficChart').getContext('2d');
        if (charts.traffic) charts.traffic.destroy();

        charts.traffic = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [
                    { label: 'Chat Messages', data: msgs, backgroundColor: '#3498DB' },
                    { label: 'Voice Mins', data: vc, backgroundColor: '#9b59b6' }
                ]
            },
            options: { responsive: true, maintainAspectRatio: false }
        });
    }

    function drawChannelChart(channelAggr) {
        let ctx = document.getElementById('channelChart').getContext('2d');
        if (charts.channels) charts.channels.destroy();

        let sorted = Object.keys(channelAggr).sort((a,b) => channelAggr[b] - channelAggr[a]).slice(0,5);
        let data = sorted.map(k => channelAggr[k]);
        
        // Fallback for empty data
        if(sorted.length === 0) {
           sorted = ["No Data"];
           data = [1];
        }

        charts.channels = new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: sorted,
                datasets: [{
                    data: data,
                    backgroundColor: sorted[0] === 'No Data' ? ['#2A2A2A'] : ['#F2C21A', '#e67e22', '#e74c3c', '#9b59b6', '#3498db'],
                    borderWidth: 0
                }]
            },
            options: { responsive: true, maintainAspectRatio: false }
        });
    }

    function drawOps(latestRow) {
        let ops = document.getElementById('ops-container');
        ops.innerHTML = "";
        if (!latestRow || !latestRow.granular_json) {
            ops.innerHTML = "<div style='color:var(--text-muted); font-size:0.9rem; padding: 1rem;'>Waiting for midnight sync (granular data empty for this day).</div>";
            return;
        }
        
        let g = latestRow.granular_json;
        let html = "";
        
        html += `<div class="param-row"><span class="param-name">Quests Completed</span><span class="param-req req-false">GAMEPLAY</span><span class="param-type">${g.quests_completed || 0}</span></div>`;
        html += `<div class="param-row"><span class="param-name">New Tickets</span><span class="param-req req-true">SUPPORT</span><span class="param-type">${g.new_tickets || 0}</span></div>`;
        html += `<div class="param-row"><span class="param-name">Ticket Avg Rating</span><span class="param-req req-true">SUPPORT</span><span class="param-type">⭐ ${g.ticket_avg_rating || 0}/5</span></div>`;
        html += `<div class="param-row"><span class="param-name">Mod Actions</span><span class="param-req req-true">ADMIN</span><span class="param-type">${g.total_mod_actions || 0} Actions</span></div>`;
        
        ops.innerHTML = html;
    }

    // Init
    fetchStats();
});
