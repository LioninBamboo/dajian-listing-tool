// popup.js

document.addEventListener('DOMContentLoaded', () => {
    // --- Target store (sticky across collections) ---------------------------
    // Collecting happens in category batches: pick the store once, keep going.
    const storeSelect = document.getElementById('storeSelect');
    const storeHint = document.getElementById('storeHint');
    const storeBox = document.getElementById('storeBox');

    DAJIAN_STORES.forEach((s) => {
        const opt = document.createElement('option');
        opt.value = s.id;
        opt.textContent = s.name;
        storeSelect.appendChild(opt);
    });

    function paintStore(id) {
        const s = dajianStoreById(id);
        storeBox.style.borderColor = s.color;
        storeSelect.style.color = s.color;
        storeHint.textContent = `采集将发送到 localhost:${s.port}（${s.short}）。切换前采集的都会进当前店。`;
    }

    chrome.storage.local.get([DAJIAN_STORE_KEY], (result) => {
        const id = result[DAJIAN_STORE_KEY] || DAJIAN_DEFAULT_STORE;
        storeSelect.value = id;
        paintStore(id);
    });

    storeSelect.addEventListener('change', () => {
        const id = storeSelect.value;
        chrome.storage.local.set({ [DAJIAN_STORE_KEY]: id }, () => paintStore(id));
    });

    // --- Profit calculator ---------------------------------------------------
    const costInput = document.getElementById('cost');
    const shippingInput = document.getElementById('shipping');
    const marginInput = document.getElementById('margin');
    const calcBtn = document.getElementById('calcBtn');
    const resultArea = document.getElementById('resultArea');
    const finalPriceEl = document.getElementById('finalPrice');
    const statusEl = document.getElementById('status');

    // Load saved settings
    chrome.storage.local.get(['defaultMargin', 'defaultShipping'], (result) => {
        if (result.defaultMargin) marginInput.value = result.defaultMargin;
        if (result.defaultShipping) shippingInput.value = result.defaultShipping;
    });

    // Try to get price from current tab content using scripting (Advanced)
    // For now, simpliest is manual input, but we can try to grab active tab selected text

    calcBtn.addEventListener('click', () => {
        const cost = parseFloat(costInput.value) || 0;
        const shipping = parseFloat(shippingInput.value) || 0;
        const marginPercent = parseFloat(marginInput.value) || 30;

        if (cost <= 0) {
            statusEl.textContent = "Please enter valid cost.";
            statusEl.style.color = "red";
            return;
        }

        // Formula: 
        // SellingPrice = (Cost + Shipping) / (1 - FeeRate - MarginRate)
        // eBay Fee = ~13% + 0.30 fixed (Simplified to 13%)
        // Margin = Profit / SellingPrice

        const feeRate = 0.13;
        const marginRate = marginPercent / 100;

        // Denominator must be positive
        const denominator = 1 - feeRate - marginRate;

        let sellingPrice = 0;
        if (denominator > 0) {
            sellingPrice = (cost + shipping) / denominator;
        } else {
            statusEl.textContent = "Impossible margin (Fees + Margin >= 100%)";
            statusEl.style.color = "red";
            return;
        }

        finalPriceEl.textContent = `$${sellingPrice.toFixed(2)}`;
        resultArea.style.display = 'block';
        statusEl.textContent = "Saved settings locally.";
        statusEl.style.color = "green";

        // Save defaults
        chrome.storage.local.set({
            defaultMargin: marginPercent,
            defaultShipping: shipping
        });
    });
});
