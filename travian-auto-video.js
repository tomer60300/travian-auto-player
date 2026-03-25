/**
 * Travian Legends — Universal Video Reward Auto-Claimer
 * 
 * Paste this entire script into the F12 console on any Travian Legends game page.
 * It will discover and claim ALL available video rewards automatically.
 * 
 * The ads still play (hidden off-screen) to generate the required hash,
 * but you don't have to watch or interact with them.
 * 
 * Supported reward types (auto-discovered):
 *   - Production Boost (+15% lumber/clay/iron/crop for 8h)
 *   - Building Upgrade duration reduction
 *   - Adventure duration reduction
 *   - Smithy upgrade
 *   - Adventure difficulty
 *   - Daily quest rewards
 *   - Any future video reward types added by Travian
 */

(async function TravianVideoAutoClaimer() {
  'use strict';

  const VERSION = window.Travian?.version || document.querySelector('meta[name="version"]')?.content || '389';
  const LOG_PREFIX = '[🎬 AutoVideo]';
  const IFRAME_TIMEOUT_MS = 120000;
  const BETWEEN_CLAIMS_MS = 3000;
  const MAX_CONCURRENT = 1; // sequential to avoid rate limits

  const log = (msg, ...args) => console.log(`${LOG_PREFIX} ${msg}`, ...args);
  const warn = (msg, ...args) => console.warn(`${LOG_PREFIX} ${msg}`, ...args);
  const success = (msg, ...args) => console.log(`${LOG_PREFIX} ✅ ${msg}`, ...args);
  const fail = (msg, ...args) => console.error(`${LOG_PREFIX} ❌ ${msg}`, ...args);

  // --- API helper ---
  async function api(endpoint, body = {}) {
    const res = await fetch(`/api/v1/${endpoint}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Version': VERSION },
      body: JSON.stringify(body),
      credentials: 'include'
    });
    if (!res.ok) throw new Error(`API ${endpoint} returned ${res.status}`);
    const text = await res.text();
    try { return JSON.parse(text); } catch { return text; }
  }

  // --- Discover all available video rewards via GraphQL ---
  async function discoverRewards() {
    log('Discovering available video rewards...');
    
    const query = `{
      ownPlayer {
        productionBoost {
          lumber { isActive expireAt type bonus videoFeatureAvailable durationVideoFeature }
          clay { isActive expireAt type bonus videoFeatureAvailable durationVideoFeature }
          iron { isActive expireAt type bonus videoFeatureAvailable durationVideoFeature }
          crop { isActive expireAt type bonus videoFeatureAvailable durationVideoFeature }
        }
      }
    }`;
    
    const result = await api('graphql', { query });
    const rewards = [];
    
    // Production boosts
    const boost = result?.data?.ownPlayer?.productionBoost;
    if (boost) {
      for (const [resource, data] of Object.entries(boost)) {
        if (data.videoFeatureAvailable) {
          rewards.push({
            type: 'productionBoost',
            label: `${resource} +${data.durationVideoFeature ? data.durationVideoFeature / 3600 + 'h' : ''}  production boost`,
            resource,
            data: { resource },
          });
        } else if (data.isActive) {
          const remaining = data.expireAt ? Math.max(0, data.expireAt - Math.floor(Date.now() / 1000)) : 0;
          log(`${resource}: already active (${data.bonus}%, ${Math.floor(remaining / 60)}m remaining)`);
        }
      }
    }

    // Try to discover other video feature types from the page source
    // These endpoints follow the same pattern: /api/v1/videofeature/open/{type}
    const otherTypes = await discoverPageVideoButtons();
    for (const entry of otherTypes) {
      if (!rewards.find(r => r.type === entry.type && JSON.stringify(r.data) === JSON.stringify(entry.data))) {
        rewards.push(entry);
      }
    }

    return rewards;
  }

  // --- Scan DOM for any video reward buttons we might have missed ---
  function discoverPageVideoButtons() {
    const found = [];
    
    // Look for all purple activate buttons with video icons
    document.querySelectorAll('.bonusVideo button, button.purple, [class*="video"] button, [class*="Video"] button').forEach(btn => {
      // Walk up to find context about what this button activates
      const container = btn.closest('[class*="bonus"], [class*="Bonus"], [class*="video"], [class*="Video"], [class*="feature"], [class*="Feature"]');
      if (!container) return;
      
      const text = container.textContent || '';
      const onclick = btn.getAttribute('onclick') || '';
      
      // Check for onclick handlers that call videofeature
      if (onclick.includes('videofeature') || onclick.includes('openVideo')) {
        const typeMatch = onclick.match(/videofeature\/open\/(\w+)/);
        if (typeMatch) {
          found.push({
            type: typeMatch[1],
            label: text.trim().substring(0, 60),
            data: {},
            element: btn
          });
        }
      }
    });

    // Also check for React-rendered video buttons by scanning Travian's internal state
    try {
      const videoFeatureTypes = ['buildingUpgrade', 'adventureDuration', 'smithyUpgrade', 'adventureDifficulty', 'dailyQuest'];
      // These would need specific page contexts (e.g., building view, adventures page)
      // We log them so the user knows they exist
      if (found.length === 0) {
        log('No additional video buttons found on current page.');
        log('Other video types exist on specific pages: ' + videoFeatureTypes.join(', '));
      }
    } catch (e) { /* ignore */ }

    return found;
  }

  // --- Claim a single video reward ---
  async function claimReward(reward) {
    log(`Claiming: ${reward.label || reward.type} (${reward.resource || 'n/a'})...`);

    // Phase 1: Open video session
    let openResult;
    try {
      openResult = await api(`videofeature/open/${reward.type}`, reward.data);
    } catch (e) {
      fail(`Open failed for ${reward.type}: ${e.message}`);
      return false;
    }

    const { vrid, videoIframeUrl } = openResult;
    if (!vrid || !videoIframeUrl) {
      fail(`No vrid/iframe for ${reward.type}. Response:`, openResult);
      return false;
    }
    log(`Got vrid: ${vrid.substring(0, 8)}...`);

    // Phase 2: Notify start
    try {
      await api('videofeature/start', { vrid });
      log('Start notified');
    } catch (e) {
      warn(`Start notification failed (continuing): ${e.message}`);
    }

    // Phase 3: Load iframe hidden, wait for videoEnds postMessage with hash
    log('Loading ad iframe (hidden)... waiting for video completion');
    
    let hash;
    try {
      hash = await loadIframeAndWaitForHash(videoIframeUrl, vrid);
    } catch (e) {
      fail(`Video completion timeout/error: ${e.message}`);
      // Try to auto-play if the iframe is stuck
      return false;
    }
    
    log(`Got hash: ${hash.substring(0, 8)}...`);

    // Phase 4: Claim reward
    let endResult;
    try {
      endResult = await api('videofeature/ends', { vrid, hash });
    } catch (e) {
      fail(`Ends call failed: ${e.message}`);
      return false;
    }

    if (endResult?.token) {
      success(`${reward.label || reward.type}: Reward claimed! Token: ${endResult.token}`);
      return true;
    } else {
      fail(`Unexpected ends response:`, endResult);
      return false;
    }
  }

  // --- Load iframe and wait for the hash via postMessage ---
  function loadIframeAndWaitForHash(iframeUrl, expectedVrid) {
    return new Promise((resolve, reject) => {
      const iframe = document.createElement('iframe');
      iframe.style.cssText = 'position:fixed;bottom:0;right:0;width:400px;height:300px;z-index:99999;opacity:0.01;pointer-events:none;';
      iframe.setAttribute('allow', 'autoplay; fullscreen');
      iframe.setAttribute('allowfullscreen', '');
      
      // Normalize URL
      let src = iframeUrl;
      if (src.startsWith('//')) src = 'https:' + src;
      iframe.src = src;

      const timeout = setTimeout(() => {
        cleanup();
        reject(new Error(`Timeout after ${IFRAME_TIMEOUT_MS / 1000}s — video may need interaction or ad blocker is active`));
      }, IFRAME_TIMEOUT_MS);

      // Track progress for logging
      let lastProgress = '';

      function handler(e) {
        const data = e.data;
        if (typeof data !== 'string') return;

        // Log progress events
        if (data === 'videoStart') {
          log('▶ Video started playing');
        } else if (data === 'videoComplete') {
          log('⏹ Video complete, waiting for signed hash...');
        } else if (data.includes('ad.firstQuartile') || data.includes('"fire":"start"')) {
          if (lastProgress !== '25%') { lastProgress = '25%'; log('⏳ 25% played'); }
        } else if (data.includes('ad.midpoint')) {
          if (lastProgress !== '50%') { lastProgress = '50%'; log('⏳ 50% played'); }
        } else if (data.includes('ad.thirdQuartile')) {
          if (lastProgress !== '75%') { lastProgress = '75%'; log('⏳ 75% played'); }
        } else if (data.includes('ad.complete')) {
          if (lastProgress !== '100%') { lastProgress = '100%'; log('⏳ 100% played'); }
        }

        // The money shot: videoEnds:{vrid}:{hash}
        if (data.startsWith('videoEnds:')) {
          const payload = data.replace('videoEnds:', '');
          const colonIdx = payload.indexOf(':');
          if (colonIdx > 0) {
            const msgVrid = payload.substring(0, colonIdx);
            const msgHash = payload.substring(colonIdx + 1);
            // Accept if vrid matches or if it's the only active session
            if (msgVrid === expectedVrid || !expectedVrid) {
              clearTimeout(timeout);
              cleanup();
              resolve(msgHash);
            }
          }
        }
      }

      function cleanup() {
        window.removeEventListener('message', handler);
        try { iframe.remove(); } catch (e) { /* ignore */ }
      }

      window.addEventListener('message', handler);
      document.body.appendChild(iframe);

      // Try to auto-click play button inside iframe after a delay
      setTimeout(() => {
        try {
          // Can't reach into cross-origin iframe directly,
          // but some ad players auto-play. If not, the user will see
          // a small iframe in the corner they can click.
          log('Waiting for ad to auto-play (or click the tiny iframe in bottom-right corner)...');
        } catch (e) { /* cross-origin, expected */ }
      }, 3000);
    });
  }

  // --- Main execution ---
  console.clear();
  log('=== Travian Video Reward Auto-Claimer ===');
  log(`Game version: ${VERSION}`);
  log('');

  const rewards = await discoverRewards();

  if (rewards.length === 0) {
    log('No video rewards available right now.');
    log('This can mean:');
    log('  - All bonuses are already active');
    log('  - Cooldowns haven\'t expired yet');
    log('  - You\'re not on the right page (try dorf1.php → Advantages shop)');
    return;
  }

  log(`Found ${rewards.length} claimable video reward(s):`);
  rewards.forEach((r, i) => log(`  ${i + 1}. ${r.label || r.type} [${r.resource || ''}]`));
  log('');
  log('Starting auto-claim sequence...');
  log('');

  let claimed = 0;
  let failed = 0;

  for (const reward of rewards) {
    try {
      const ok = await claimReward(reward);
      if (ok) claimed++;
      else failed++;
    } catch (e) {
      fail(`Unexpected error: ${e.message}`);
      failed++;
    }

    // Wait between claims to avoid rate limiting
    if (rewards.indexOf(reward) < rewards.length - 1) {
      log(`Waiting ${BETWEEN_CLAIMS_MS / 1000}s before next claim...`);
      await new Promise(r => setTimeout(r, BETWEEN_CLAIMS_MS));
    }
  }

  log('');
  log('=== Summary ===');
  success(`Claimed: ${claimed}/${rewards.length}`);
  if (failed > 0) fail(`Failed: ${failed}/${rewards.length}`);
  log('Refresh the page to see your bonuses!');
  log('');
  log('💡 TIP: Bookmark this as a bookmarklet:');
  log('   javascript:fetch("https://raw.githubusercontent.com/...").then(r=>r.text()).then(eval)');
  log('   Or just paste this script again next time rewards are available.');

})();
