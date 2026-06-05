const tg = window.Telegram?.WebApp;

let currentPath = "";
let currentFiles = [];
let selectedPaths = new Set();
let activeTab = "files";
let selectionSummaryTimer = null;
let zipPollTimer = null;
let cachedSettings = {};

document.addEventListener("DOMContentLoaded", () => {
    initTelegramApp();
    loadFiles();
    loadDownloads();
    loadStats();
    loadSettings();
    loadZipJobs();
    setInterval(loadDownloads, 3000);
    setInterval(loadStats, 10000);
});

function initTelegramApp() {
    if (!tg) return;
    tg.ready();
    tg.expand();
    tg.enableClosingConfirmation();
    tg.setHeaderColor("#e0e5ec");
    tg.setBackgroundColor("#e0e5ec");
}

function apiHeaders(extra = {}) {
    return {
        "X-Init-Data": tg?.initData || "",
        ...extra,
    };
}

function telegramUserId() {
    return tg?.initDataUnsafe?.user?.id || null;
}

function identityPayload(extra = {}) {
    const userId = telegramUserId();
    return {
        ...(userId ? { user_id: userId, chat_id: userId } : {}),
        ...extra,
    };
}

function withIdentityQuery(url) {
    const userId = telegramUserId();
    if (!userId) return url;
    const separator = url.includes("?") ? "&" : "?";
    return `${url}${separator}user_id=${encodeURIComponent(userId)}&chat_id=${encodeURIComponent(userId)}`;
}

function switchTab(tabId, element) {
    if (tabId === activeTab) return;
    const current = document.getElementById(`tab-${activeTab}`);
    activeTab = tabId;
    if (current) {
        current.classList.add("is-leaving");
        setTimeout(() => current.classList.remove("active", "is-leaving"), 180);
    }
    document.querySelectorAll(".nav-item").forEach((nav) => nav.classList.remove("active"));

    const next = document.getElementById(`tab-${tabId}`);
    next.classList.add("active");
    element.classList.add("active");
    element.classList.add("animating");
    setTimeout(() => element.classList.remove("animating"), 340);
    document.getElementById("header-title").innerText = element.getAttribute("data-title");

    if (tabId === "downloads") loadDownloads();
    if (tabId === "info") loadStats();
    if (tabId === "settings") loadSettings();
}

async function loadFiles() {
    const list = document.getElementById("file-list");
    list.innerHTML = `<div class="empty-state">Loading files...</div>`;

    try {
        const response = await fetch(`/api/files?path=${encodeURIComponent(currentPath)}`, {
            headers: apiHeaders(),
        });
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);

        currentPath = data.current_path || "";
        currentFiles = data.items || [];
        renderBreadcrumbs();
        renderFiles();
        updateBackButton();
        updateSelectionSummary();
        await loadStats();
    } catch (error) {
        list.innerHTML = `<div class="empty-state">Could not load files.<br>${escapeHtml(error.message)}</div>`;
    }
}

function renderFiles() {
    const list = document.getElementById("file-list");
    if (currentFiles.length === 0) {
        list.innerHTML = `<div class="empty-state">This directory is empty.</div>`;
        return;
    }

    list.innerHTML = currentFiles
        .map((item) => {
            const isFolder = item.type === "folder";
            const icon = isFolder ? "fa-folder-closed" : iconForFile(item.name);
            const size = isFolder ? "Folder" : item.size_readable || formatBytes(item.size || 0);
            const modified = item.modified ? new Date(item.modified).toLocaleDateString() : "";
            const checked = selectedPaths.has(item.path) ? "checked" : "";
            return `
                <div class="file-item neu-out" onclick="handleRowClick('${jsString(item.path)}', ${isFolder})">
                    <div class="file-info">
                        <div class="file-icon neu-in ${isFolder ? "folder-icon" : ""}">
                            <i class="fas ${icon}"></i>
                        </div>
                        <div class="file-details">
                            <h4>${escapeHtml(item.name)}</h4>
                            <p>${escapeHtml(size)} &bull; ${escapeHtml(modified)}</p>
                        </div>
                    </div>
                    <label class="checkbox-wrapper" onclick="event.stopPropagation()">
                        <input type="checkbox" class="file-checkbox" value="${escapeHtml(item.path)}" ${checked}
                            onchange="toggleSelection('${jsString(item.path)}', this.checked)">
                        <div class="checkmark"></div>
                    </label>
                </div>
            `;
        })
        .join("");
}

function handleRowClick(path, isFolder) {
    if (isFolder) {
        currentPath = path;
        loadFiles();
        return;
    }
    const next = !selectedPaths.has(path);
    toggleSelection(path, next);
    renderFiles();
}

function toggleSelection(path, checked) {
    if (checked) selectedPaths.add(path);
    else selectedPaths.delete(path);
    updateSelectionSummary();
}

function navigateUp() {
    if (!currentPath) return;
    const parts = currentPath.split("/");
    parts.pop();
    currentPath = parts.join("/");
    loadFiles();
}

function jumpToPath(path) {
    currentPath = path;
    loadFiles();
}

function renderBreadcrumbs() {
    const container = document.getElementById("breadcrumbs");
    const parts = currentPath ? currentPath.split("/").filter(Boolean) : [];
    const crumbs = [`<span class="breadcrumb-item" onclick="jumpToPath('')">Root</span>`];
    let path = "";

    parts.forEach((part) => {
        path += (path ? "/" : "") + part;
        crumbs.push(`<span class="breadcrumb-separator">/</span>`);
        crumbs.push(`<span class="breadcrumb-item" onclick="jumpToPath('${jsString(path)}')">${escapeHtml(part)}</span>`);
    });

    container.innerHTML = crumbs.join("");
}

function updateBackButton() {
    const backBtn = document.getElementById("back-btn");
    backBtn.disabled = !currentPath;
    backBtn.style.opacity = currentPath ? "1" : "0.4";
}

async function deleteFiles() {
    const paths = Array.from(selectedPaths);
    if (paths.length === 0) {
        toast("Select files or folders first");
        return;
    }
    if (!window.confirm(`Delete ${paths.length} selected item(s)?`)) return;

    toast("Deleting...");
    try {
        const response = await fetch("/api/files/delete", {
            method: "POST",
            headers: apiHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify(identityPayload({ paths })),
        });
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || "Delete failed");
        toast(data.message || "Deleted");
        selectedPaths.clear();
        await loadFiles();
    } catch (error) {
        toast(`Delete failed: ${error.message}`);
    }
}

async function zipFiles() {
    const paths = Array.from(selectedPaths);
    if (paths.length === 0) {
        toast("Select files or folders first");
        return;
    }

    const name = `archive_${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "")}`;
    toast("Starting ZIP and upload job...");
    try {
        const response = await fetch("/api/files/zip-upload", {
            method: "POST",
            headers: apiHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify(identityPayload({ paths, name })),
        });
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || "ZIP failed");
        toast("ZIP job started. Watch progress in Downloads.");
        selectedPaths.clear();
        loadFiles();
        loadZipJobs();
        const downloadsNav = document.querySelector('[data-title="Active Downloads"]');
        if (downloadsNav) switchTab("downloads", downloadsNav);
    } catch (error) {
        toast(`ZIP failed: ${error.message}`);
    }
}

async function uploadSelectedFiles() {
    const paths = Array.from(selectedPaths);
    if (paths.length === 0) {
        toast("Select files or folders first");
        return;
    }

    toast(`Preparing ${paths.length} selected item(s) for upload...`);
    try {
        const response = await fetch("/api/files/upload-selected", {
            method: "POST",
            headers: apiHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify(identityPayload({ paths })),
        });
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || "Upload failed");
        toast(data.message || `Uploading ${data.file_count || paths.length} file(s)`);
        selectedPaths.clear();
        renderFiles();
        updateSelectionSummary();
    } catch (error) {
        toast(`Upload failed: ${error.message}`);
    }
}

function clearSelection() {
    selectedPaths.clear();
    renderFiles();
    updateSelectionSummary();
}

function updateSelectionSummary() {
    clearTimeout(selectionSummaryTimer);
    selectionSummaryTimer = setTimeout(loadSelectionSummary, 120);
}

async function loadSelectionSummary() {
    const summary = document.getElementById("selection-summary");
    if (!selectedPaths.size) {
        summary.classList.add("hidden");
        return;
    }

    summary.classList.remove("hidden");
    document.getElementById("selected-count").innerText = "Calculating...";
    document.getElementById("selected-size").innerText = `${selectedPaths.size} selected item(s)`;

    try {
        const response = await fetch("/api/files/selection-summary", {
            method: "POST",
            headers: apiHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify(identityPayload({ paths: Array.from(selectedPaths) })),
        });
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || "Summary failed");
        const label = data.file_count === 1 ? "1 file" : `${data.file_count} files`;
        document.getElementById("selected-count").innerText = label;
        document.getElementById("selected-size").innerText = `${data.total_size || "0 B"} selected`;
    } catch (error) {
        document.getElementById("selected-count").innerText = `${selectedPaths.size} selected`;
        document.getElementById("selected-size").innerText = "Could not calculate size";
    }
}

function showDownloadDialog() {
    document.getElementById("download-source").value = "";
    document.getElementById("download-modal").classList.remove("hidden");
}

function closeDownloadDialog() {
    document.getElementById("download-modal").classList.add("hidden");
}

async function startDownloads() {
    const sources = document
        .getElementById("download-source")
        .value.split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean);

    if (sources.length === 0) {
        toast("Paste at least one URL or magnet link");
        return;
    }

    closeDownloadDialog();
    toast(`Starting ${sources.length} download(s)...`);
    try {
        const response = await fetch("/api/downloads/start", {
            method: "POST",
            headers: apiHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify(identityPayload({ sources })),
        });
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || "Could not start download");
        const started = data.started?.length || 0;
        const failed = data.errors?.length || 0;
        toast(`Started ${started}; failed ${failed}`);
        loadDownloads();
    } catch (error) {
        toast(`Start failed: ${error.message}`);
    }
}

async function loadDownloads() {
    try {
        const response = await fetch("/api/downloads", { headers: apiHeaders() });
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || "Downloads unavailable");
        renderDownloads(data.jobs || []);
        document.getElementById("dl-speed").innerText = formatSpeed(data.total_down_speed || 0);
        loadZipJobs();
    } catch (error) {
        document.getElementById("download-list").innerHTML =
            `<div class="empty-state">Could not load downloads.<br>${escapeHtml(error.message)}</div>`;
    }
}

function renderDownloads(downloads) {
    const list = document.getElementById("download-list");
    const zipJobs = window.latestZipJobs || [];
    if (downloads.length === 0 && zipJobs.length === 0) {
        list.innerHTML = `<div class="empty-state">No downloads yet.</div>`;
        return;
    }

    const zipHtml = zipJobs.map(renderZipJob).join("");
    const downloadHtml = downloads
        .map((dl) => {
            const progress = Math.max(0, Math.min(100, Number(dl.progress || 0)));
            const isPaused = dl.status === "paused";
            const isFinished = ["completed", "failed", "cancelled"].includes(dl.status);
            const toggleAction = isPaused ? "resume" : "pause";
            const toggleIcon = isPaused ? "fa-play" : "fa-pause";
            const toggleText = isPaused ? "Resume" : "Pause";
            return `
                <div class="download-item neu-out">
                    <div class="dl-header">
                        <span>${escapeHtml(dl.name || "Unknown download")}</span>
                        <span>${progress.toFixed(1)}%</span>
                    </div>
                    <div class="progress-track neu-in">
                        <div class="progress-bar" style="width: ${progress}%"></div>
                    </div>
                    <div class="dl-status">
                        ${escapeHtml(titleCase(dl.status || "unknown"))} &bull;
                        ${escapeHtml(dl.completed_readable || "0 B")} / ${escapeHtml(dl.total_readable || "0 B")}<br>
                        Down ${formatSpeed(dl.download_speed || 0)} &bull; Up ${formatSpeed(dl.upload_speed || 0)} &bull;
                        ETA ${escapeHtml(dl.eta || "Unknown")}
                    </div>
                    ${
                        isFinished
                            ? ""
                            : `<div class="download-controls">
                                <button class="neu-btn text-accent" onclick="controlDownload(${dl.id}, '${toggleAction}')">
                                    <i class="fas ${toggleIcon}"></i> ${toggleText}
                                </button>
                                <button class="neu-btn text-danger" onclick="controlDownload(${dl.id}, 'cancel')">
                                    <i class="fas fa-stop"></i> Cancel
                                </button>
                            </div>`
                    }
                </div>
            `;
        })
        .join("");
    list.innerHTML = zipHtml + downloadHtml;
}

function renderZipJob(job) {
    const done = ["completed", "failed"].includes(job.status);
    const icon = job.status === "failed" ? "fa-triangle-exclamation" : done ? "fa-circle-check" : "fa-file-zipper";
    return `
        <div class="download-item neu-out">
            <div class="dl-header">
                <span><i class="fas ${icon}"></i> ZIP Upload</span>
                <span>${escapeHtml(titleCase(job.phase || job.status || "queued"))}</span>
            </div>
            <div class="progress-track neu-in">
                <div class="progress-bar zip-pulse" style="width: ${done ? 100 : 55}%"></div>
            </div>
            <div class="dl-status">
                ${escapeHtml(job.progress_text || "Queued...")}<br>
                ${Number(job.file_count || 0)} file(s) &bull; ${escapeHtml(job.total_size || "0 B")}
            </div>
        </div>
    `;
}

async function loadZipJobs() {
    try {
        const response = await fetch("/api/zip-jobs", { headers: apiHeaders() });
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || "ZIP jobs unavailable");
        window.latestZipJobs = data.jobs || [];
        if (activeTab === "downloads") {
            const responseDownloads = await fetch("/api/downloads", { headers: apiHeaders() });
            const downloadsData = await responseDownloads.json();
            renderDownloads(downloadsData.jobs || []);
        }
        clearTimeout(zipPollTimer);
        if (window.latestZipJobs.some((job) => !["completed", "failed"].includes(job.status))) {
            zipPollTimer = setTimeout(loadZipJobs, 2000);
        }
    } catch (error) {
        console.error(error);
    }
}

async function controlDownload(jobId, action) {
    toast(`${titleCase(action)} job #${jobId}...`);
    try {
        const response = await fetch(`/api/downloads/${jobId}/${action}`, {
            method: "POST",
            headers: apiHeaders(),
        });
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || "Action failed");
        toast(data.message || "Done");
        loadDownloads();
    } catch (error) {
        toast(`${titleCase(action)} failed: ${error.message}`);
    }
}

async function loadStats() {
    try {
        const [statsResponse, downloadsResponse] = await Promise.all([
            fetch("/api/stats", { headers: apiHeaders() }),
            fetch("/api/downloads", { headers: apiHeaders() }),
        ]);
        const stats = await statsResponse.json();
        const downloads = await downloadsResponse.json();
        if (!statsResponse.ok || stats.error) throw new Error(stats.error || "Stats failed");

        const totalItems = (stats.file_count || 0) + (stats.folder_count || 0);
        const totalBytes = stats.total_size_bytes || 0;
        const displayLimit = Math.max(totalBytes, 1 * 1024 * 1024 * 1024);
        const percentage = Math.min(100, Math.round((totalBytes / displayLimit) * 100));

        document.getElementById("total-count").innerText = totalItems.toLocaleString();
        document.getElementById("storage-percentage").innerText = `${percentage}%`;
        document.getElementById("storage-ratio").innerText = stats.total_size || "0 B";
        document.getElementById("storage-summary").innerText =
            `${stats.file_count || 0} files, ${stats.folder_count || 0} folders in the download tree.`;
        document.getElementById("dl-speed").innerText = formatSpeed(downloads.total_down_speed || 0);
    } catch (error) {
        console.error(error);
    }
}

async function loadSettings() {
    const list = document.getElementById("settings-list");
    if (!list) return;

    try {
        const response = await fetch(withIdentityQuery("/api/settings"), { headers: apiHeaders() });
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || "Settings unavailable");
        cachedSettings = data.settings || {};
        renderSettings();
    } catch (error) {
        list.innerHTML = `<div class="empty-state">Could not load settings.<br>${escapeHtml(error.message)}</div>`;
    }
}

function renderSettings() {
    const settings = cachedSettings;
    const partSizeMb = Number(settings.zip_part_size || 0) / (1024 * 1024);
    document.getElementById("settings-list").innerHTML = `
        ${settingSelect("Archive Part Size", "Split archives into Telegram-friendly volumes.", "zip_part_size", [
            [268435456, "256 MB"],
            [536870912, "512 MB"],
            [1073741824, "1 GB"],
            [2147483648, "2 GB"],
        ], settings.zip_part_size || 1073741824, `${partSizeMb || 1024} MB`)}
        ${settingSelect("Archive Method", "Choose ZIP for speed or 7Z for stronger compression.", "zip_method", [
            ["zip", "ZIP"],
            ["7z", "7Z"],
        ], settings.zip_method || "zip")}
        ${settingSelect("Compression Level", "Higher levels are smaller but slower.", "compression_level", [
            [1, "1"],
            [3, "3"],
            [5, "5"],
            [7, "7"],
            [9, "9"],
        ], settings.compression_level || 3)}
        ${settingPassword(settings.password || "")}
        ${settingToggle("Auto-delete after ZIP", "Delete source files after ZIP/upload finishes.", "auto_delete_files_after_zip", settings.auto_delete_files_after_zip)}
        ${settingToggle("Delete ZIPs after send", "Remove generated archives after Telegram upload.", "auto_delete_zips_after_send", settings.auto_delete_zips_after_send)}
        ${settingToggle("Auto-delete after upload", "Delete selected source files after direct upload.", "auto_delete_files_after_upload", settings.auto_delete_files_after_upload)}
        ${settingToggle("Forwarded posts", "Automatically download forwarded Telegram media.", "auto_download_forwarded_posts", settings.auto_download_forwarded_posts)}
        ${settingToggle("Manga auto PDF", "Convert downloaded manga image folders to PDF automatically.", "manga_auto_convert_pdf", settings.manga_auto_convert_pdf)}
        ${settingToggle("Remove manga images", "Delete manga images after PDF conversion finishes.", "manga_remove_images_after_conversion", settings.manga_remove_images_after_conversion)}
    `;
}

function settingSelect(title, description, key, options, value, fallbackLabel = "") {
    return `
        <div class="setting-item neu-out">
            <div class="setting-copy">
                <h4>${escapeHtml(title)}</h4>
                <p>${escapeHtml(description)}</p>
            </div>
            <select class="setting-select neu-in" onchange="saveSetting('${key}', this.value)">
                ${options.map(([optionValue, label]) => `
                    <option value="${optionValue}" ${String(optionValue) === String(value) ? "selected" : ""}>${escapeHtml(label)}</option>
                `).join("")}
                ${fallbackLabel && !options.some(([optionValue]) => String(optionValue) === String(value)) ? `<option selected>${escapeHtml(fallbackLabel)}</option>` : ""}
            </select>
        </div>
    `;
}

function settingToggle(title, description, key, value) {
    return `
        <div class="setting-item neu-out">
            <div class="setting-copy">
                <h4>${escapeHtml(title)}</h4>
                <p>${escapeHtml(description)}</p>
            </div>
            <button class="neu-btn setting-toggle ${value ? "active" : ""}" onclick="saveSetting('${key}', ${value ? "false" : "true"})" aria-label="${escapeHtml(title)}"></button>
        </div>
    `;
}

function settingPassword(value) {
    return `
        <div class="setting-item neu-out">
            <div class="setting-copy">
                <h4>Archive Password</h4>
                <p>${value ? "Password is set." : "Leave empty for no password."}</p>
            </div>
            <button class="neu-btn setting-control" onclick="promptPassword()">Set</button>
        </div>
    `;
}

async function promptPassword() {
    const value = window.prompt("Archive password. Leave empty to remove it.", cachedSettings.password || "");
    if (value === null) return;
    await saveSetting("password", value);
}

async function saveSetting(key, value) {
    const numericKeys = new Set(["zip_part_size", "compression_level"]);
    const booleanKeys = new Set([
        "auto_delete_files_after_zip",
        "auto_delete_zips_after_send",
        "auto_delete_files_after_upload",
        "auto_download_forwarded_posts",
        "manga_auto_convert_pdf",
        "manga_remove_images_after_conversion",
    ]);
    let normalized = value;
    if (numericKeys.has(key)) normalized = Number(value);
    if (booleanKeys.has(key)) normalized = value === true || value === "true";

    try {
        const response = await fetch("/api/settings", {
            method: "POST",
            headers: apiHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify(identityPayload({ key, value: normalized })),
        });
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || "Save failed");
        cachedSettings = data.settings || {};
        renderSettings();
        toast("Settings saved");
    } catch (error) {
        toast(`Settings failed: ${error.message}`);
        renderSettings();
    }
}

function iconForFile(name) {
    if (/\.(jpg|jpeg|png|gif|webp)$/i.test(name)) return "fa-image";
    if (/\.(zip|rar|tar|gz|7z)$/i.test(name)) return "fa-file-zipper";
    if (/\.(mp4|mkv|mov|avi|webm)$/i.test(name)) return "fa-file-video";
    if (/\.(mp3|wav|flac|aac|ogg)$/i.test(name)) return "fa-file-audio";
    if (/\.(json|txt|py|html|css|js|ts|md)$/i.test(name)) return "fa-file-code";
    if (/\.(pdf|doc|docx)$/i.test(name)) return "fa-file-lines";
    return "fa-file";
}

function formatBytes(bytes) {
    const value = Number(bytes || 0);
    if (!value) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
    return `${(value / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function formatSpeed(bytesPerSecond) {
    return `${formatBytes(bytesPerSecond)}/s`;
}

function titleCase(text) {
    return String(text).replace(/_/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function toast(message) {
    const el = document.getElementById("toast");
    el.textContent = message;
    el.classList.remove("hidden");
    clearTimeout(window.toastTimer);
    window.toastTimer = setTimeout(() => el.classList.add("hidden"), 2600);
}

function escapeHtml(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function jsString(value) {
    return String(value ?? "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
}
