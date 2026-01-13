// background.js - Service Worker for API Requests

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === "collectProduct") {

        console.log("Background: Sending data to server...", request.data.sku);

        fetch("http://localhost:8000/api/collect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(request.data)
        })
            .then(async response => {
                // Check if response is OK
                if (!response.ok) {
                    console.error(`Background: HTTP Error ${response.status}`);
                }

                // Try to parse as JSON
                const contentType = response.headers.get("content-type");
                if (contentType && contentType.includes("application/json")) {
                    return response.json();
                } else {
                    // Not JSON, get text
                    const text = await response.text();
                    console.error("Background: Non-JSON response:", text.substring(0, 200));
                    throw new Error(`Server returned non-JSON response: ${text.substring(0, 100)}`);
                }
            })
            .then(data => {
                console.log("Background: Success", data);
                sendResponse({ status: "success", data: data });
            })
            .catch(error => {
                console.error("Background: Error", error);
                sendResponse({ status: "error", message: error.toString() });
            });

        return true; // Keep the message channel open for async response
    }
});
