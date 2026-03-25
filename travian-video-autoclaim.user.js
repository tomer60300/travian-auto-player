// ==UserScript==
// @name         Travian Video Auto-Skip & Claim v4
// @namespace    openclaw
// @version      4.0
// @description  Auto-watches and claims Travian video rewards. Skips when possible, claims ASAP.
// @match        https://*.travian.com/*
// @match        https://*.kingdoms.com/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  const TAG = '[🎬 v4]';
  const POLL_MS = 1500;       // check every 1.5s
  const CLICK_DELAY = 800;    // small delay before clicking to let UI settle

  const log = (...args) => console.log(TAG, ...args);
  const warn = (...args) => console.warn(TAG, ...args);

  log('=== Travian Video Auto-Skip & Claim v4 ===');

  // --- Helpers ---
  function click(el, label) {
    if (!el) return false;
    log(`Clicking: ${label}`);
    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return true;
  }

  function queryAll(selector, root = document) {
    return [...root.querySelectorAll(selector)];
  }

  function findByText(selector, text, root = document) {
    return queryAll(selector, root).find(el =>
      el.textContent.trim().toLowerCase().includes(text.toLowerCase())
    );
  }

  // Try to find elements inside iframes too
  function queryAllDeep(selector) {
    let results = queryAll(selector);
    try {
      document.querySelectorAll('iframe').forEach(iframe => {
        try {
          const doc = iframe.contentDocument || iframe.contentWindow?.document;
          if (doc) results = results.concat(queryAll(selector, doc));
        } catch (e) { /* cross-origin, skip */ }
      });
    } catch (e) {}
    return results;
  }

  // --- Core Logic ---
  let claimCount = 0;
  let lastState = '';

  function setState(s) {
    if (s !== lastState) { log(s); lastState = s; }
  }

  function tick() {
    try {
      // 1. Look for "Collect" / "Claim" / reward buttons
      const collectBtn =
        findByText('button', 'collect') ||
        findByText('button', 'claim') ||
        findByText('button', 'reward') ||
        findByText('a', 'collect') ||
        findByText('a', 'claim') ||
        document.querySelector('.rewardButton, .collectReward, .videoRewardButton, [class*="collect"], [class*="reward"][class*="btn"], [class*="Reward"][class*="Btn"]') ||
        findByText('button', 'abholen') ||  // German
        findByText('button', 'récupérer');  // French

      if (collectBtn && collectBtn.offsetParent !== null) {
        setState(`🎁 Reward button found! (claim #${claimCount + 1})`);
        setTimeout(() => {
          click(collectBtn, 'Collect/Claim reward');
          claimCount++;
          log(`✅ Claimed reward #${claimCount}`);
        }, CLICK_DELAY);
        return;
      }

      // 2. Look for close (X) button on completed video dialog
      const closeBtn =
        document.querySelector('.dialogClose, .closeButton, .videoClose, [class*="close"][class*="dialog"], .modalClose') ||
        document.querySelector('.videoDialog .close, .adDialog .close, .rewardOverlay .close');

      // Only click close if there's no active video playing
      const videoPlaying = isVideoActive();

      // 3. Check for "watch video" / "start" buttons to auto-start next
      const watchBtn =
        findByText('button', 'watch') ||
        findByText('button', 'video') ||
        findByText('button', 'play') ||
        findByText('a', 'watch') ||
        document.querySelector('[class*="watchVideo"], [class*="playVideo"], [class*="startVideo"]') ||
        findByText('button', 'ansehen') ||  // German
        findByText('button', 'regarder');   // French

      // 4. Ad counter / skip detection
      const adText = queryAll('*').find(el => {
        const t = el.textContent.trim();
        return /Ad Counter[:\s]*0/i.test(t) || /skip/i.test(t);
      });

      if (adText) {
        const skipBtn = findByText('button', 'skip') ||
          findByText('a', 'skip') ||
          findByText('span', 'skip') ||
          document.querySelector('[class*="skip"], [class*="Skip"]');
        if (skipBtn && skipBtn.offsetParent !== null) {
          setState('⏭️ Skip button available!');
          setTimeout(() => click(skipBtn, 'Skip ad'), CLICK_DELAY);
          return;
        }
      }

      // 5. If video is done and close button visible, close it
      if (!videoPlaying && closeBtn && closeBtn.offsetParent !== null) {
        setState('✖️ Video done, closing dialog...');
        setTimeout(() => click(closeBtn, 'Close dialog'), CLICK_DELAY);
        return;
      }

      // 6. If a "watch video" button is available, click to start
      if (watchBtn && watchBtn.offsetParent !== null && !videoPlaying) {
        setState('▶️ Starting next video...');
        setTimeout(() => click(watchBtn, 'Watch video'), CLICK_DELAY * 2);
        return;
      }

      // 7. Monitor active video
      if (videoPlaying) {
        setState('📺 Video playing... waiting for completion');
        return;
      }

      setState('💤 Idle — no video dialog detected');
    } catch (e) {
      warn('Error in tick:', e);
    }
  }

  function isVideoActive() {
    // Check for video elements or iframes that look like ad players
    const videos = queryAllDeep('video');
    for (const v of videos) {
      if (!v.paused && !v.ended && v.currentTime > 0) return true;
    }
    // Check for ad counter text that's still counting
    const adCounter = queryAll('*').find(el => /Ad Counter[:\s]*[1-9]/i.test(el.textContent.trim()));
    if (adCounter) return true;

    // Check for iframes that might be video players
    const iframes = document.querySelectorAll('iframe[src*="video"], iframe[src*="ad"], iframe[class*="video"], iframe[class*="ad"]');
    if (iframes.length > 0) return true;

    return false;
  }

  // --- MutationObserver for faster reaction ---
  const observer = new MutationObserver(() => {
    // Quick check on DOM changes
    setTimeout(tick, 300);
  });

  observer.observe(document.body, {
    childList: true,
    subtree: true,
    attributes: false,
  });

  // --- Main loop ---
  setInterval(tick, POLL_MS);

  // Initial tick
  setTimeout(tick, 2000);

  log(`Started. Polling every ${POLL_MS}ms. Claims so far: ${claimCount}`);
})();
