(function(){
  const isAdmin = location.pathname.startsWith('/admin');
  const ws = new WebSocket(`ws://${location.host}/ws`);

  // ======== Общие элементы ========
  const $ = (id)=> document.getElementById(id);
  const byId = $;

  // ======== Игрок (index.html) ========
  if (!isAdmin){
    let roomCode = null;
    let playerId = null;
    let roundsTotal = 0;
    let timerInt = null;
    let timeLeft = 0;

    const ui = {
      joinCard: $('joinCard'),
      lobbyCard: $('lobbyCard'),
      playersWrap: $('playersWrap'),
      questionCard: $('questionCard'),
      revealCard: $('revealCard'),
      finalCard: $('finalCard'),
      roomChip: $('roomChip'),
      roomCode: $('roomCode'),
      playerName: $('playerName'),
      btnJoin: $('btnJoin'),
      roundNo: $('roundNo'),
      roundsTotal: $('roundsTotal'),
      cat: $('cat'),
      cat2: $('cat2'),
      prompt: $('prompt'),
      answerText: $('answerText'),
      btnSend: $('btnSend'),
      timer: $('timer'),
      revealBody: $('revealBody'),
      finalBody: $('finalBody'),
      dlMeCsv: $('dlMeCsv')
    };

    ui.btnJoin.onclick = ()=>{
      const code = (ui.roomCode.value || '').toUpperCase().trim();
      const name = (ui.playerName.value || '').trim();
      if (code.length !== 4) { alert('Код — 4 символа'); return; }
      if (!name) { alert('Введите имя'); return; }
      roomCode = code;
      ws.send(JSON.stringify({type:'join', roomCode: code, playerName: name}));
    };

    ui.btnSend.onclick = ()=>{
      if (!roomCode || !playerId) return;
      const text = ui.answerText.value.trim();
      ws.send(JSON.stringify({type:'answer', roomCode, playerId, text}));
      ui.btnSend.disabled = true;
    };

    ws.onmessage = (ev)=>{
      const m = JSON.parse(ev.data);
      switch(m.type){
        case 'error':
          alert(m.message || 'Ошибка'); break;

        case 'joined':
          roomCode = m.roomCode;
          playerId = m.playerId;
          ui.roomChip.textContent = `Комната: ${roomCode}`;
          ui.joinCard.style.display = 'none';
          renderPlayers(ui.playersWrap, m.players);
          ui.lobbyCard.style.display = 'block';
          break;

        case 'players':
          renderPlayers(ui.playersWrap, m.players);
          break;

        case 'game_started':
          roundsTotal = m.rounds;
          ui.lobbyCard.style.display = 'none';
          break;

        case 'question':
          ui.revealCard.style.display = 'none';
          ui.finalCard.style.display = 'none';
          ui.questionCard.style.display = 'block';
          ui.roundNo.textContent = m.round;
          ui.roundsTotal.textContent = m.totalRounds;
          ui.cat.textContent = m.category;
          ui.prompt.textContent = m.prompt;
          ui.answerText.value = '';
          ui.answerText.focus();
          ui.btnSend.disabled = false;
          // таймер 40–60
          if (timerInt) clearInterval(timerInt);
          timeLeft = m.timeLimit;
          ui.timer.textContent = String(timeLeft);
          timerInt = setInterval(()=>{
            timeLeft -= 1;
            ui.timer.textContent = String(timeLeft);
            if (timeLeft <= 0){ clearInterval(timerInt); }
          }, 1000);
          break;

        case 'reveal':
          if (timerInt) clearInterval(timerInt);
          ui.questionCard.style.display = 'none';
          ui.revealCard.style.display = 'block';
          ui.correctText.textContent = m.correct || '';
          ui.cat2.textContent = m.category || '';
          ui.revealBody.innerHTML = '';
          (m.results||[]).forEach(r=>{
            const tr = document.createElement('tr');
            tr.innerHTML = `<td>${esc(r.name)}</td>
                            <td>${esc(r.text || '')}</td>
                            <td>${r.isCorrect ? 'Да' : 'Нет'}</td>
                            <td>${r.awarded || 0}</td>
                            <td>${r.timeMs || 0}</td>
                            <td><b>${r.score || 0}</b></td>`;
            ui.revealBody.appendChild(tr);
          });
          break;

        case 'final':
          ui.questionCard.style.display = 'none';
          ui.revealCard.style.display = 'none';
          ui.finalCard.style.display = 'block';
          ui.finalBody.innerHTML = '';
          (m.scores || []).sort((a,b)=> (b.score||0)-(a.score||0)).forEach(p=>{
            const tr = document.createElement('tr');
            tr.innerHTML = `<td>${esc(p.name)}</td><td class="score">${p.score || 0}</td>`;
            ui.finalBody.appendChild(tr);
          });
          if (roomCode && playerId){
            ui.dlMeCsv.href = `/api/export/${roomCode}/player/${playerId}.csv`;
          }
          break;
      }
    };

    function renderPlayers(target, players){
      target.innerHTML = '';
      const ul = document.createElement('ul');
      ul.className = 'list';
      players.forEach(p=>{
        const li = document.createElement('li');
        li.className = 'player';
        li.innerHTML = `<div class="name">${esc(p.name)}</div><div class="score">${p.score||0}</div>`;
        ul.appendChild(li);
      });
      target.appendChild(ul);
    }

    function esc(s){ return String(s).replaceAll('&','&amp;').replaceAll('<','&lt;'); }
  }

  // ======== Админ (admin.html) ========
  if (isAdmin){
    let roomCode = null;

    const ui = {
      adminRoom: $('adminRoom'),
      prefCode: $('prefCode'),
      rounds: $('rounds'),
      btnCreate: $('btnCreate'),
      players: $('players'),
      roomCard: $('roomCard'),
      roomInfo: $('roomInfo'),
      btnStart: $('btnStart'),
      btnEnd: $('btnEnd'),
      taskSummary: $('taskSummary'),
      btnReload: $('btnReload'),
      btnFetchResults: $('btnFetchResults'),
      btnExportRoom: $('btnExportRoom'),
      results: $('results')
    };

    ui.btnCreate.onclick = ()=>{
      const preferredCode = (ui.prefCode.value || '').toUpperCase().trim();
      const rounds = parseInt(ui.rounds.value || '6', 10) || 6;
      ws.send(JSON.stringify({type:'admin_create_room', preferredCode, rounds}));
    };

    ui.btnStart.onclick = ()=>{
      if (!roomCode) return;
      ws.send(JSON.stringify({type:'admin_start', roomCode}));
    };

    ui.btnEnd.onclick = ()=>{
      if (!roomCode) return;
      ws.send(JSON.stringify({type:'admin_end', roomCode}));
    };

    ui.btnReload.onclick = async ()=>{
      const r = await fetch('/api/tasks/reload', { method:'POST' });
      const j = await r.json();
      if (j.ok){
        renderTaskSummary();
        alert('tasks.json перезагружен');
      } else {
        alert('Ошибка: ' + (j.error||''));
      }
    };

    ui.btnFetchResults.onclick = async ()=>{
      if (!roomCode){ alert('Комната не создана'); return; }
      const r = await fetch(`/api/room/${roomCode}/results`);
      if (!r.ok){ ui.results.textContent = 'Нет данных'; return; }
      const j = await r.json();
      const players = j.players || [];
      const answers = j.answers || [];
      ui.results.innerHTML = `
        <div>Статус: <b>${j.status}</b> • Раундов: <b>${j.rounds}</b></div>
        <div style="margin-top:6px"><b>Участники:</b></div>
        <ul class="list">${players.map(p=>`<li class="player"><div>${esc(p.name)}</div><div class="score">${p.score}</div></li>`).join('')}</ul>
        <div class="hint" style="margin-top:8px">Ответов записано: ${answers.length}</div>
      `;
      ui.btnExportRoom.href = `/api/export/${roomCode}/room.csv`;
      ui.btnExportRoom.download = `${roomCode}_summary.csv`;
    };

    ws.onopen = ()=>{
      // просто ждём событий; если комната уже была — можно было бы сделать admin_attach
    };

    ws.onmessage = (ev)=>{
      const m = JSON.parse(ev.data);
      switch(m.type){
        case 'error':
          alert(m.message || 'Ошибка'); break;

        case 'room_created':
          roomCode = m.roomCode;
          ui.adminRoom.textContent = `Комната: ${roomCode}`;
          ui.roomCard.style.display = 'block';
          ui.roomInfo.textContent = `Комната создана: ${roomCode}`;
          renderTaskSummary();
          break;

        case 'room_attached':
          roomCode = m.roomCode;
          ui.adminRoom.textContent = `Комната: ${roomCode}`;
          ui.roomCard.style.display = 'block';
          renderPlayers(m.players||[]);
          break;

        case 'players':
          renderPlayers(m.players||[]);
          break;

        case 'game_started':
          ui.roomInfo.textContent = `Сессия запущена • Раундов: ${m.rounds}`;
          break;

        case 'question':
          ui.roomInfo.textContent = `Раунд ${m.round}/${m.totalRounds} • Категория: ${m.category} • ${m.timeLimit} сек`;
          break;

        case 'reveal':
          ui.roomInfo.textContent = `Итоги раунда ${m.round} • правильный: ${m.correct}`;
          break;

        case 'final':
          ui.roomInfo.textContent = `Сессия завершена`;
          break;
      }
    };

    async function renderTaskSummary(){
      const r = await fetch('/api/tasks');
      if (!r.ok){ ui.taskSummary.textContent = 'Не удалось получить задачи'; return; }
      const j = await r.json();
      const cats = j.categories || {};
      const total = j.total || 0;
      ui.taskSummary.innerHTML = `Всего задач: <b>${total}</b><br>` +
        Object.entries(cats).map(([k,v])=> `${esc(k)} — <b>${v}</b>`).join('<br>');
    }

    function renderPlayers(players){
      ui.players.innerHTML = '';
      players.forEach(p=>{
        const li = document.createElement('li');
        li.className = 'player';
        li.innerHTML = `<div class="name">${esc(p.name)}</div><div class="score">${p.score||0}</div>`;
        ui.players.appendChild(li);
      });
    }

    function esc(s){ return String(s).replaceAll('&','&amp;').replaceAll('<','&lt;'); }
  }
})();
