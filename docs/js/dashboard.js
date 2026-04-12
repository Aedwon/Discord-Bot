document.addEventListener('DOMContentLoaded', () => {
    const loader = document.getElementById('loader');
    const rangeLabel = document.getElementById('range-label');

    // Preset buttons
    const presets = {
        '7d':        document.getElementById('btn-7d'),
        '14d':       document.getElementById('btn-14d'),
        '30d':       document.getElementById('btn-30d'),
        'week':      document.getElementById('btn-this-week'),
        'lastweek':  document.getElementById('btn-last-week'),
        'month':     document.getElementById('btn-this-month'),
        'year':      document.getElementById('btn-this-year'),
    };
    const dateStartEl = document.getElementById('date-start');
    const dateEndEl   = document.getElementById('date-end');

    let rawData = [];
    let charts  = {};

    // ── Chart.js Global Config ──
    Chart.defaults.color = '#999';
    Chart.defaults.font.family = 'Inter';
    Chart.defaults.plugins.legend.labels.boxWidth = 12;
    Chart.defaults.plugins.legend.labels.padding = 16;
    Chart.defaults.scales.linear = Chart.defaults.scales.linear || {};

    const gridColor = 'rgba(255,255,255,0.04)';
    const tickColor = '#666';

    // ── Utility ──
    function toStr(d) {
        const y = d.getFullYear();
        const m = String(d.getMonth() + 1).padStart(2, '0');
        const day = String(d.getDate()).padStart(2, '0');
        return `${y}-${m}-${day}`;
    }

    function setActive(key) {
        Object.values(presets).forEach(b => b.classList.remove('active'));
        if (key && presets[key]) presets[key].classList.add('active');
    }

    // ── Date Presets (ISO 8601 Monday-Sunday) ──
    function setDateBounds(preset) {
        const today = new Date();
        let s = new Date(), e = new Date();

        switch(preset) {
            case '7d':   s.setDate(today.getDate() - 6); break;
            case '14d':  s.setDate(today.getDate() - 13); break;
            case '30d':  s.setDate(today.getDate() - 29); break;
            case 'week': {
                let dow = today.getDay() || 7;
                s.setDate(today.getDate() - dow + 1);
                break;
            }
            case 'lastweek': {
                let dow = today.getDay() || 7;
                s.setDate(today.getDate() - dow - 6);
                e.setDate(today.getDate() - dow);
                break;
            }
            case 'month':
                s = new Date(today.getFullYear(), today.getMonth(), 1);
                break;
            case 'year':
                s = new Date(today.getFullYear(), 0, 1);
                break;
        }

        dateStartEl.value = toStr(s);
        dateEndEl.value   = toStr(e);
        renderDashboard();
    }

    // ── Event Listeners ──
    Object.entries(presets).forEach(([key, btn]) => {
        btn.addEventListener('click', () => { setActive(key); setDateBounds(key); });
    });
    dateStartEl.addEventListener('change', () => { setActive(null); renderDashboard(); });
    dateEndEl.addEventListener('change',   () => { setActive(null); renderDashboard(); });

    // ── Fetch ──
    async function fetchStats() {
        try {
            loader.innerText = "⏳ Syncing...";
            loader.style.color = "var(--accent)";
            const res = await fetch('/api/stats');
            const body = await res.json();
            if (body.success && body.data) {
                rawData = body.data.sort((a, b) => a.date.localeCompare(b.date));
                loader.innerText = "● Connected";
                loader.style.color = "var(--green)";
                setDateBounds('7d');
                setActive('7d');
            } else throw new Error("Bad payload");
        } catch(err) {
            console.error(err);
            loader.innerText = "✕ Connection failed";
            loader.style.color = "var(--red)";
        }
    }

    // ── Render ──
    function renderDashboard() {
        const sStr = dateStartEl.value, eStr = dateEndEl.value;
        const d = rawData.filter(r => (!sStr || r.date >= sStr) && (!eStr || r.date <= eStr));

        rangeLabel.textContent = d.length > 0
            ? `${d[0].date} → ${d[d.length-1].date}  (${d.length} day${d.length !== 1 ? 's' : ''})`
            : 'No data in range';

        // ── Aggregate core KPIs ──
        let sumMsgs=0, sumVc=0, sumJoins=0, sumLeaves=0, sumReactions=0;
        let sumVerif=0, sumQuests=0, sumQuiz=0, sumQuizPts=0, sumThanks=0, sumEP=0, sumRefs=0;
        let sumMod=0, sumTickets=0, sumTixRatings=0, tixRatingSum=0;
        let maxUniqueMsg=0, maxUniqueVc=0;
        let sumEventRegs=0, sumEventClaims=0, sumEpDist=0, sumRaffleEntries=0, sumRafflesCreated=0, sumBoosterWins=0;

        let labels=[], joinsArr=[], leavesArr=[], netArr=[], msgArr=[], vcArr=[];
        let questArr=[], quizArr=[], thanksArr=[], modArr=[];
        let channelAggr={}, ticketCatAggr={};

        for (const row of d) {
            sumMsgs += row.total_messages || 0;
            sumVc   += row.total_voice_minutes || 0;
            sumJoins  += row.new_joins || 0;
            sumLeaves += row.new_leaves || 0;
            sumReactions += row.total_reactions || 0;
            maxUniqueMsg = Math.max(maxUniqueMsg, row.unique_messagers || 0);
            maxUniqueVc  = Math.max(maxUniqueVc,  row.unique_voice_users || 0);

            labels.push(row.date);
            joinsArr.push(row.new_joins || 0);
            leavesArr.push(row.new_leaves || 0);
            netArr.push((row.new_joins || 0) - (row.new_leaves || 0));
            msgArr.push(row.total_messages || 0);
            vcArr.push(row.total_voice_minutes || 0);

            const g = row.granular_json;
            if (g) {
                sumVerif  += g.new_verifications || 0;
                sumQuests += g.quests_completed || 0;
                sumQuiz   += g.quiz_sessions || 0;
                sumQuizPts += g.quiz_score || 0;
                sumThanks += g.thanks_given || 0;
                sumEP     += g.ep_redemptions || 0;
                sumRefs   += g.new_referrals || 0;
                sumMod    += g.total_mod_actions || 0;
                sumTickets += g.new_tickets || 0;
                sumTixRatings += g.ticket_ratings_count || 0;
                tixRatingSum += (g.ticket_avg_rating || 0) * (g.ticket_ratings_count || 0);

                sumEventRegs += g.event_registrations || 0;
                sumEventClaims += g.event_participation_claims || 0;
                sumEpDist += g.event_ep_distributed || 0;
                sumRaffleEntries += g.event_raffle_entries || 0;
                sumRafflesCreated += g.event_raffles_created || 0;
                sumBoosterWins += g.booster_raffle_wins || 0;

                questArr.push(g.quests_completed || 0);
                quizArr.push(g.quiz_sessions || 0);
                thanksArr.push(g.thanks_given || 0);
                modArr.push(g.total_mod_actions || 0);

                (g.top_text_channels || []).forEach(ch => {
                    let k = `#${ch.channel_id}`;
                    channelAggr[k] = (channelAggr[k] || 0) + (ch.count || 0);
                });
                if (g.tickets_by_category) {
                    Object.entries(g.tickets_by_category).forEach(([cat, cnt]) => {
                        ticketCatAggr[cat] = (ticketCatAggr[cat] || 0) + cnt;
                    });
                }
            } else {
                questArr.push(0); quizArr.push(0); thanksArr.push(0); modArr.push(0);
            }
        }

        // ── Populate KPIs ──
        setText('stat-msgs', sumMsgs.toLocaleString());
        setText('stat-vc', sumVc.toLocaleString());
        const net = sumJoins - sumLeaves;
        setText('stat-growth', `${net >= 0 ? '+' : ''}${net.toLocaleString()}`);
        setText('stat-reactions', sumReactions.toLocaleString());
        setText('stat-verifs', sumVerif.toLocaleString());
        setText('stat-unique', maxUniqueMsg.toLocaleString());

        setText('sub-msgs', `${(d.length > 0 ? Math.round(sumMsgs/d.length) : 0).toLocaleString()} avg/day`);
        setText('sub-vc',   `${(d.length > 0 ? Math.round(sumVc/d.length) : 0).toLocaleString()} avg/day`);
        setText('sub-growth', `▲${sumJoins} joined · ▼${sumLeaves} left`);
        setText('sub-unique', `${maxUniqueVc} voice users peak`);

        setText('stat-quests', sumQuests.toLocaleString());
        setText('stat-quiz', sumQuiz.toLocaleString());
        setText('stat-quiz-pts', sumQuizPts.toLocaleString());
        setText('stat-thanks', sumThanks.toLocaleString());
        setText('stat-ep', sumEP.toLocaleString());
        setText('stat-referrals', sumRefs.toLocaleString());

        setText('stat-mod', sumMod.toLocaleString());
        setText('stat-tickets', sumTickets.toLocaleString());
        setText('stat-tix-ratings', sumTixRatings.toLocaleString());
        const avgRating = sumTixRatings > 0 ? (tixRatingSum / sumTixRatings).toFixed(1) : '0.0';
        setText('stat-tix-avg', `⭐ ${avgRating}`);

        setText('stat-event-regs', sumEventRegs.toLocaleString());
        setText('stat-event-claims', sumEventClaims.toLocaleString());
        setText('stat-ep-dist', sumEpDist.toLocaleString());
        setText('stat-raffle-entries', sumRaffleEntries.toLocaleString());
        setText('stat-raffles-created', sumRafflesCreated.toLocaleString());
        setText('stat-booster-wins', sumBoosterWins.toLocaleString());

        // ── Draw Charts ──
        drawGrowth(labels, joinsArr, leavesArr, netArr);
        drawMsg(labels, msgArr);
        drawVc(labels, vcArr);
        drawEconomy(labels, questArr, quizArr, thanksArr);
        drawChannels(channelAggr);
        drawMod(labels, modArr);
        drawTickets(ticketCatAggr);

        // ── Draw Detail Tables ──
        const latest = d.length > 0 ? d[d.length - 1] : null;
        drawQuizTable(latest);
        drawThanksTable(latest);
        drawInvitesTable(latest);
        drawModTable(latest);
        drawRetentionTable(latest);
    }

    function setText(id, val) { document.getElementById(id).innerText = val; }

    // ── Chart Drawing Functions ──
    function makeOpts(extra = {}) {
        return {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { position: 'top' } },
            scales: {
                x: { grid: { color: gridColor }, ticks: { color: tickColor, maxRotation: 45, font: { size: 10 } } },
                y: { grid: { color: gridColor }, ticks: { color: tickColor, font: { size: 10 } }, beginAtZero: true }
            },
            ...extra
        };
    }

    function drawGrowth(labels, joins, leaves, net) {
        if (charts.growth) charts.growth.destroy();
        charts.growth = new Chart(document.getElementById('growthChart'), {
            type: 'line',
            data: { labels, datasets: [
                { label: 'Net Growth', data: net, borderColor: '#F2C21A', backgroundColor: 'rgba(242,194,26,0.08)', fill: true, tension: 0.35, borderWidth: 2, pointRadius: 3 },
                { label: 'Joins', data: joins, borderColor: '#43b581', borderDash: [4,4], tension: 0.35, borderWidth: 1.5, pointRadius: 2 },
                { label: 'Leaves', data: leaves, borderColor: '#f04747', borderDash: [4,4], tension: 0.35, borderWidth: 1.5, pointRadius: 2 },
            ]},
            options: makeOpts()
        });
    }

    function drawMsg(labels, msgs) {
        if (charts.msg) charts.msg.destroy();
        charts.msg = new Chart(document.getElementById('msgChart'), {
            type: 'bar',
            data: { labels, datasets: [
                { label: 'Messages', data: msgs, backgroundColor: 'rgba(52,152,219,0.7)', borderRadius: 4, borderSkipped: false },
            ]},
            options: makeOpts()
        });
    }

    function drawVc(labels, vc) {
        if (charts.vc) charts.vc.destroy();
        charts.vc = new Chart(document.getElementById('vcChart'), {
            type: 'bar',
            data: { labels, datasets: [
                { label: 'Voice Mins', data: vc, backgroundColor: 'rgba(155,89,182,0.7)', borderRadius: 4, borderSkipped: false },
            ]},
            options: makeOpts()
        });
    }

    function drawEconomy(labels, quests, quiz, thanks) {
        if (charts.economy) charts.economy.destroy();
        charts.economy = new Chart(document.getElementById('economyChart'), {
            type: 'line',
            data: { labels, datasets: [
                { label: 'Quests', data: quests, borderColor: '#2ecc71', tension: 0.3, borderWidth: 2, pointRadius: 3 },
                { label: 'Quiz', data: quiz, borderColor: '#e67e22', tension: 0.3, borderWidth: 2, pointRadius: 3 },
                { label: 'Thanks', data: thanks, borderColor: '#e74c3c', tension: 0.3, borderWidth: 2, pointRadius: 3 },
            ]},
            options: makeOpts()
        });
    }

    function drawChannels(aggr) {
        if (charts.channels) charts.channels.destroy();
        let keys = Object.keys(aggr).sort((a,b) => aggr[b] - aggr[a]).slice(0,6);
        let vals = keys.map(k => aggr[k]);
        if (!keys.length) { keys = ['No Data']; vals = [1]; }
        const colors = ['#F2C21A','#e67e22','#e74c3c','#9b59b6','#3498db','#2ecc71'];
        charts.channels = new Chart(document.getElementById('channelChart'), {
            type: 'doughnut',
            data: { labels: keys, datasets: [{ data: vals, backgroundColor: keys[0]==='No Data' ? ['#2A2A2A'] : colors, borderWidth: 0 }] },
            options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'bottom', labels: { font: { size: 10 } } } } }
        });
    }

    function drawMod(labels, mod) {
        if (charts.mod) charts.mod.destroy();
        charts.mod = new Chart(document.getElementById('modChart'), {
            type: 'bar',
            data: { labels, datasets: [
                { label: 'Mod Actions', data: mod, backgroundColor: 'rgba(231,76,60,0.6)', borderRadius: 4, borderSkipped: false },
            ]},
            options: makeOpts()
        });
    }

    function drawTickets(aggr) {
        if (charts.tickets) charts.tickets.destroy();
        let keys = Object.keys(aggr);
        let vals = keys.map(k => aggr[k]);
        if (!keys.length) { keys = ['No Tickets']; vals = [1]; }
        const colors = ['#3498db','#2ecc71','#F2C21A','#9b59b6','#e67e22','#e74c3c'];
        charts.tickets = new Chart(document.getElementById('ticketChart'), {
            type: 'doughnut',
            data: { labels: keys, datasets: [{ data: vals, backgroundColor: keys[0]==='No Tickets' ? ['#2A2A2A'] : colors, borderWidth: 0 }] },
            options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'bottom', labels: { font: { size: 10 } } } } }
        });
    }

    // ── Detail Table Renderers ──
    function renderRows(containerId, items, emptyMsg) {
        const el = document.getElementById(containerId);
        if (!items || items.length === 0) {
            el.innerHTML = `<div class="empty-state">${emptyMsg}</div>`;
            return;
        }
        el.innerHTML = items.map((item, i) =>
            `<div class="detail-row"><span><span class="rank">#${i+1}</span><span class="label">${item.label}</span></span><span class="value">${item.value}</span></div>`
        ).join('');
    }

    function drawQuizTable(row) {
        const g = row?.granular_json;
        const items = (g?.quiz_top_3 || []).map(q => ({ label: q.name || `User ${q.user_id}`, value: `${(q.score || 0).toLocaleString()} pts` }));
        renderRows('table-quiz', items, 'No quiz activity for this day');
    }

    function drawThanksTable(row) {
        const g = row?.granular_json;
        const items = (g?.thanks_top_3 || []).map(t => ({ label: t.name || `User ${t.user_id}`, value: `${t.count}× thanked` }));
        renderRows('table-thanks', items, 'No thanks activity for this day');
    }

    function drawInvitesTable(row) {
        const g = row?.granular_json;
        const items = (g?.top_invites || []).map(i => ({ label: `${i.code} (by ${i.name || i.inviter})`, value: `${i.count} joins` }));
        renderRows('table-invites', items, 'No invite data for this day');
    }

    function drawModTable(row) {
        const g = row?.granular_json;
        if (!g || !g.mod_actions || Object.keys(g.mod_actions).length === 0) {
            renderRows('table-mod', [], 'No moderation actions');
            return;
        }
        const items = Object.entries(g.mod_actions).map(([action, count]) => ({ label: action, value: count }));
        renderRows('table-mod', items, 'No moderation actions');
    }

    function drawRetentionTable(row) {
        const g = row?.granular_json;
        const ret = g?.retention_day_1;
        if (!ret) {
            renderRows('table-retention', [], 'No retention data');
            return;
        }
        const items = [
            { label: 'Day-1 Retention Rate', value: `${ret.rate || 0}%` },
            { label: 'Retained Members', value: `${ret.retained || 0}` },
            { label: 'Joined (Cohort)', value: `${ret.joined || 0}` },
        ];
        renderRows('table-retention', items, 'No retention data');
    }

    // ── Init ──
    fetchStats();
});
