'use strict';

const { query } = require('../database/postgres');

async function loadHistory(participantId, limit = 30) {
  const res = await query(
    `SELECT role, content
     FROM chat_history
     WHERE participant_id = $1
     ORDER BY created_at DESC
     LIMIT $2`,
    [participantId, limit * 2]
  );
  // Rows come back newest-first; reverse to chronological order
  return res.rows.reverse().map(r => ({ role: r.role, content: r.content }));
}

async function saveExchange(participantId, participantName, participantType, channelId, userMsg, assistantMsg) {
  await query(
    `INSERT INTO chat_history (participant_id, participant_name, participant_type, channel_id, role, content)
     VALUES ($1,$2,$3,$4,'user',$5), ($1,$2,$3,$4,'assistant',$6)`,
    [participantId, participantName, participantType, channelId, userMsg, assistantMsg]
  );
}

async function getParticipantSummary(participantId) {
  const res = await query(
    `SELECT participant_name, participant_type,
            COUNT(*) AS message_count,
            MIN(created_at) AS first_seen,
            MAX(created_at) AS last_seen
     FROM chat_history
     WHERE participant_id = $1
     GROUP BY participant_name, participant_type`,
    [participantId]
  );
  if (!res.rows.length) return null;
  const r = res.rows[0];
  return {
    name:         r.participant_name,
    type:         r.participant_type,
    messageCount: parseInt(r.message_count),
    firstSeen:    r.first_seen,
    lastSeen:     r.last_seen,
  };
}

module.exports = { loadHistory, saveExchange, getParticipantSummary };
