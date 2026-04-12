document.addEventListener('DOMContentLoaded', () => {
    const loader = document.getElementById('loader');
    const daysInput = document.getElementById('days-limit');
    
    let rawData = [];
    let charts = {};

    Chart.defaults.color = '#CCCCCC';
    Chart.defaults.font.family = 'Inter';

    async function fetchStats() {
        try {
            loader.innerText = "Syncing secure connection...";
            loader.style.color = "var(--accent)";
            
            const response = await fetch('/api/stats');
            const resData = await response.json();
            
            if (resData.success && resData.data) {
                // Ensure dates are sorted chronologically
                rawData = resData.data.sort((a, b) => new Date(a.date) - new Date(b.date));
                loader.innerText = "● Live synced";
                loader.style.color = "var(--green)";
                renderDashboard();
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
        const limit = parseInt(daysInput.value) || 14;
        const d = rawData.slice(-limit); // Keep last N days
        if (d.length === 0) return;

        // Cumulative Totals for Header
        let sumMsgs = 0, sumVc = 0, sumJoins = 0, sumLeaves = 0, sumVerif = 0;
        let labels = [];
        let joinsData = [], leavesData = [], netData = [];
        let msgData = [], vcData = [];
        let channelAggr = {};

        // Parse metrics over the sliced range
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

            // Granular parsing
            if (row.granular_json) {
                sumVerif += row.granular_json.new_verifications || 0;
                
                // Aggregate top text channels
                if (row.granular_json.top_text_channels) {
                    row.granular_json.top_text_channels.forEach(ch => {
                        let chName = `#${ch.channel_id}`; // Generic fallback
                        channelAggr[chName] = (channelAggr[chName] || 0) + (ch.count || 0);
                    });
                }
            }
        }

        document.getElementById('stat-msgs').innerText = sumMsgs.toLocaleString();
        document.getElementById('stat-vc').innerText = sumVc.toLocaleString();
        document.getElementById('stat-growth').innerText = `${sumJoins > sumLeaves ? '+' : ''}${(sumJoins - sumLeaves).toLocaleString()} (▲${sumJoins})`;
        document.getElementById('stat-verifs').innerText = sumVerif.toLocaleString();

        drawGrowthChart(labels, joinsData, leavesData, netData);
        drawTrafficChart(labels, msgData, vcData);
        drawChannelChart(channelAggr);
        
        // Draw Quick Ops table out of the very latest row
        drawOps(d[d.length - 1]);
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

        // Sort and pick top 5
        let sorted = Object.keys(channelAggr).sort((a,b) => channelAggr[b] - channelAggr[a]).slice(0,5);
        let data = sorted.map(k => channelAggr[k]);

        charts.channels = new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: sorted,
                datasets: [{
                    data: data,
                    backgroundColor: ['#F2C21A', '#e67e22', '#e74c3c', '#9b59b6', '#3498db'],
                    borderWidth: 0
                }]
            },
            options: { responsive: true }
        });
    }

    function drawOps(latestRow) {
        let ops = document.getElementById('ops-container');
        ops.innerHTML = "";
        if (!latestRow || !latestRow.granular_json) return;
        
        let g = latestRow.granular_json;
        let html = "";
        
        html += `<div class="param-row"><span class="param-name">Quests Completed</span><span class="param-req req-false">GAMEPLAY</span><span class="param-type">${g.quests_completed || 0}</span></div>`;
        html += `<div class="param-row"><span class="param-name">New Tickets</span><span class="param-req req-true">SUPPORT</span><span class="param-type">${g.new_tickets || 0}</span></div>`;
        html += `<div class="param-row"><span class="param-name">Ticket Avg Rating</span><span class="param-req req-true">SUPPORT</span><span class="param-type">⭐ ${g.ticket_avg_rating || 0}/5</span></div>`;
        html += `<div class="param-row"><span class="param-name">Mod Actions</span><span class="param-req req-true">ADMIN</span><span class="param-type">${g.total_mod_actions || 0} Actions</span></div>`;
        
        ops.innerHTML = html;
    }

    daysInput.addEventListener('change', renderDashboard);

    // Init
    fetchStats();
});
