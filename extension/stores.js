// stores.js — the store registry shared by popup / background / content.
// Collection is done in batches by category, so the operator picks the target
// store once and it sticks. Each store instance is a separate local server.
const DAJIAN_STORES = [
    { id: "main",      name: "家具主店 AquaVerve", short: "家具主店", port: 8000, color: "#0060df" },
    { id: "autoparts", name: "汽配店 AquaRides",   short: "汽配店",   port: 8001, color: "#c2410c" },
    { id: "blindbox",  name: "盲盒店 GrovePop",    short: "盲盒店",   port: 8002, color: "#7c3aed" },
];

const DAJIAN_DEFAULT_STORE = "main";
const DAJIAN_STORE_KEY = "collectTargetStore";

function dajianStoreById(id) {
    return DAJIAN_STORES.find((s) => s.id === id) || DAJIAN_STORES[0];
}

// Available to service worker (importScripts) and to page scripts alike.
if (typeof globalThis !== "undefined") {
    globalThis.DAJIAN_STORES = DAJIAN_STORES;
    globalThis.DAJIAN_DEFAULT_STORE = DAJIAN_DEFAULT_STORE;
    globalThis.DAJIAN_STORE_KEY = DAJIAN_STORE_KEY;
    globalThis.dajianStoreById = dajianStoreById;
}
