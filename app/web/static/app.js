const tg = window.Telegram?.WebApp;

let currentPath = "";
let currentFiles = [];
let selectedPaths = new Set();
let activeTab = "files";

document.addEventListener("DOMContentLoaded", () => {
    initTelegramApp();
    loadFiles();
    loadDownloads();
    loadStats();
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

function switchTab(tabId, element) {
    activeTab = tabId;
    document.querySelectorAll(".tab-content").forEach((tab) => tab.classList.remove("active"));
    document.querySelectorAll(".nav-item").forEach((nav) => nav.classList.remove("active"));

    document.getElementById(`tab-${tabId}`).classList.add("active");
    element.classList.add("active");
    document.getElementById("header-title").innerText = element.getAttribute("data-title");

    if (tabId === "downloads") loadDownloads();
    if (tabId === "info") loadStats();
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
        selectedPaths.clear();
        renderBreadcrumbs();
        renderFiles();
        updateBackButton();
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

function triggerUpload() {
    document.getElementById("file-upload").click();
}

async function handleFileUpload(event) {
    const files = Array.from(event.target.files || []);
    if (files.length === 0) return;

    const form = new FormData();
    form.append("path", currentPath);
    files.forEach((file) => form.append("files", file));

    toast(`Uploading ${files.length} file(s)...`);
    try {
        const response = await fetch("/api/files/upload", {
            method: "POST",
            headers: apiHeaders(),
            body: form,
        });
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || "Upload failed");
        toast(data.message || "Upload complete");
        event.target.value = "";
        loadFiles();
    } catch (error) {
        toast(`Upload failed: ${error.message}`);
    }
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
            body: JSON.stringify({ paths }),
        });
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || "Delete failed");
        toast(data.message || "Deleted");
        loadFiles();
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
    toast("Creating archive...");
    try {
        const response = await fetch("/api/files/create-archive", {
            method: "POST",
            headers: apiHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify({ paths, name, format: "zip" }),
        });
        const data = await response.json();
        if (!response.ok || data.error) throw new Error(data.error || "Archive failed");
        toast(`Created ${data.archive}`);
        loadFiles();
    } catch (error) {
        toast(`Archive failed: ${error.message}`);
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
            body: JSON.stringify({ sources }),
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
    } catch (error) {
        document.getElementById("download-list").innerHTML =
            `<div class="empty-state">Could not load downloads.<br>${escapeHtml(error.message)}</div>`;
    }
}

function renderDownloads(downloads) {
    const list = document.getElementById("download-list");
    if (downloads.length === 0) {
        list.innerHTML = `<div class="empty-state">No downloads yet.</div>`;
        return;
    }

    list.innerHTML = downloads
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
