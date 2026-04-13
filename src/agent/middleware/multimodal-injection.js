'use strict';

const fs = require('fs');
const path = require('path');

const IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp']);
const MEDIA_TYPES = {
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.gif': 'image/gif', '.webp': 'image/webp',
};

/**
 * Multimodal injection middleware.
 * Detects [ATTACH: /path/to/file] markers in messages and replaces them
 * with base64 image content blocks (for charts, screenshots).
 */
async function multimodalInjection(state, next) {
  const { messages } = state;
  let modified = false;

  const processedMessages = messages.map((msg) => {
    if (msg.role !== 'user' || typeof msg.content !== 'string') return msg;

    const attachPattern = /\[ATTACH:\s*([^\]]+)\]/g;
    const attachments = [...msg.content.matchAll(attachPattern)];
    if (attachments.length === 0) return msg;

    const blocks = [];
    let lastIndex = 0;

    for (const match of attachments) {
      const filePath = match[1].trim();
      const ext = path.extname(filePath).toLowerCase();

      // Add preceding text
      if (match.index > lastIndex) {
        blocks.push({ type: 'text', text: msg.content.slice(lastIndex, match.index) });
      }

      if (IMAGE_EXTS.has(ext) && fs.existsSync(filePath)) {
        const data = fs.readFileSync(filePath).toString('base64');
        blocks.push({
          type: 'image',
          source: { type: 'base64', media_type: MEDIA_TYPES[ext], data },
        });
        modified = true;
      } else {
        blocks.push({ type: 'text', text: `[ATTACH: ${filePath} — not found or unsupported type]` });
      }

      lastIndex = match.index + match[0].length;
    }

    if (lastIndex < msg.content.length) {
      blocks.push({ type: 'text', text: msg.content.slice(lastIndex) });
    }

    return { ...msg, content: blocks };
  });

  if (modified) console.log('[multimodal] Injected image attachment(s)');
  return next({ ...state, messages: processedMessages });
}

module.exports = multimodalInjection;
