// stores.js — the store registry shared by popup / background / content.
// Collection is done in batches by category, so the operator picks the target
// store once and it sticks. Each store instance is a separate local server.
const DAJIAN_STORES = [
    { id: "main",      name: "家具主店 AquaVerve", short: "家具主店", port: 8000, color: "#0060df" },
    { id: "autoparts", name: "汽配店 AquaRides",   short: "汽配店",   port: 8001, color: "#c2410c" },
    // id was historically "blindbox"; keep that alias so sticky chrome.storage
    // selections still resolve after the outdoor pivot.
    { id: "outdoor",   name: "户外店 GrovePop",    short: "户外店",   port: 8002, color: "#2e5d43" },
];

const DAJIAN_DEFAULT_STORE = "main";
const DAJIAN_STORE_KEY = "collectTargetStore";
const DAJIAN_STORE_ALIASES = { blindbox: "outdoor" };

function dajianStoreById(id) {
    const resolved = DAJIAN_STORE_ALIASES[id] || id;
    return DAJIAN_STORES.find((s) => s.id === resolved) || DAJIAN_STORES[0];
}

// Available to service worker (importScripts) and to page scripts alike.
if (typeof globalThis !== "undefined") {
    globalThis.DAJIAN_STORES = DAJIAN_STORES;
    globalThis.DAJIAN_DEFAULT_STORE = DAJIAN_DEFAULT_STORE;
    globalThis.DAJIAN_STORE_KEY = DAJIAN_STORE_KEY;
    globalThis.DAJIAN_STORE_ALIASES = DAJIAN_STORE_ALIASES;
    globalThis.dajianStoreById = dajianStoreById;
}
