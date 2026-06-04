// Telegram Web App API
let tg = window.Telegram?.WebApp;

// State
let currentPath = '';
let selectedFiles = new Set();
let allFiles = [];
let viewMode = 'grid'; // 'grid' or 'list'

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    initTelegramApp();
    initAOS();
    loadFiles();
    attachEventListeners();
});

function initTelegramApp() {
    if (tg) {
        tg.ready();
        tg.expand();
        tg.enableClosingConfirmation();
        
        // Set header color
        tg.setHeaderColor('#3b82f6');
        tg.setBackgroundColor('#ffffff');
    }
}

function initAOS() {
    if (window.AOS) {
        AOS.init({
            duration: 400,
            once: true,
            easing: 'ease-in-out'
        });
    }
}

function attachEventListeners() {
    // Back button
    document.getElementById('backBtn').addEventListener('click', goBack);
    
    // Search
    document.getElementById('searchInput').addEventListener('keypress', (e) => {
        if (e.key === 'Enter') performSearch();
    });
    
    // Archive format - update from radio to select
    document.querySelectorAll('input[name="archiveFormat"]').forEach(radio => {
        radio.addEventListener('change', (e) => {
            // Format changed
        });
    });
}

// ==================== File Loading & Display ====================

function loadFiles() {
    const browser = document.getElementById('fileBrowser');
    const loading = document.getElementById('loadingState');
    const empty = document.getElementById('emptyState');
    const error = document.getElementById('errorState');
    
    // Show loading
    loading.classList.remove('hidden');
    browser.innerHTML = '';
    empty.classList.add('hidden');
    error.classList.add('hidden');
    
    // Disable action buttons
    document.getElementById('backBtn').disabled = true;
    
    fetch(`/api/files?path=${encodeURIComponent(currentPath)}`, {
        headers: {
            'X-Init-Data': tg?.initData || ''
        }
    })
        .then(r => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.json();
        })
        .then(data => {
            loading.classList.add('hidden');
            
            allFiles = data.items || [];
            
            if (allFiles.length === 0) {
                empty.classList.remove('hidden');
            } else {
                renderFiles(allFiles);
            }
            
            // Update breadcrumb
            updateBreadcrumb(data.current_path || '');
            
            // Update stats
            document.getElementById('fileCount').textContent = allFiles.length;
            document.getElementById('totalSize').textContent = data.total_size || '0 B';
            
            // Update back button
            document.getElementById('backBtn').disabled = !currentPath;
        })
        .catch(err => {
            console.error('Error loading files:', err);
            loading.classList.add('hidden');
            error.classList.remove('hidden');
            document.getElementById('errorMessage').textContent = err.message;
        });
}

function renderFiles(files) {
    const browser = document.getElementById('fileBrowser');
    browser.innerHTML = '';
    
    files.forEach((file, index) => {
        const item = createFileItem(file);
        item.setAttribute('data-aos', 'fade-up');
        item.setAttribute('data-aos-delay', (index * 30) % 300);
        browser.appendChild(item);
    });
    
    // Re-initialize AOS for new elements
    if (window.AOS) {
        AOS.refresh();
    }
}

function createFileItem(file) {
    const item = document.createElement('div');
    item.className = `file-item ${file.type === 'folder' ? 'folder' : ''}`;
    
    if (selectedFiles.has(file.path)) {
        item.classList.add('selected');
    }
    
    const isSelected = selectedFiles.has(file.path);
    
    item.innerHTML = `
        <div class="file-item-checkbox">${isSelected ? '✓' : '○'}</div>
        <div class="file-item-icon">${file.icon || (file.type === 'folder' ? '📁' : '📄')}</div>
        <div class="file-item-name" title="${file.name}">${file.name}</div>
        ${file.type !== 'folder' ? `<div class="file-item-size">${file.size_readable}</div>` : ''}
    `;
    
    // Click handler
    item.addEventListener('click', (e) => {
        if (file.type === 'folder') {
            navigateTo(file.path);
        } else {
            toggleFileSelection(file.path);
            item.classList.toggle('selected');
            renderFiles(allFiles);
        }
    });
    
    // Context menu for files
    if (file.type !== 'folder') {
        item.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            toggleFileSelection(file.path);
            item.classList.toggle('selected');
            renderFiles(allFiles);
        });
    }
    
    return item;
}

function updateBreadcrumb(path) {
    currentPath = path;
    const breadcrumb = document.getElementById('breadcrumb');
    breadcrumb.innerHTML = '';
    
    // Root
    const rootItem = document.createElement('span');
    rootItem.className = 'breadcrumb-item active';
    rootItem.innerHTML = '<i class="fas fa-folder"></i> Root';
    rootItem.onclick = () => navigateTo('');
    breadcrumb.appendChild(rootItem);
    
    // Parts
    if (path) {
        const parts = path.split('/');
        let current = '';
        
        parts.forEach(part => {
            if (part) {
                current += (current ? '/' : '') + part;
                
                const sep = document.createElement('span');
                sep.style.opacity = '0.5';
                sep.textContent = ' / ';
                breadcrumb.appendChild(sep);
                
                const item = document.createElement('span');
                item.className = 'breadcrumb-item';
                item.textContent = part;
                item.onclick = () => navigateTo(current);
                breadcrumb.appendChild(item);
            }
        });
    }
}

function navigateTo(path) {
    currentPath = path;
    selectedFiles.clear();
    updateSelectedCount();
    loadFiles();
}

function goBack() {
    if (currentPath) {
        const parts = currentPath.split('/');
        parts.pop();
        navigateTo(parts.join('/'));
    }
}

// ==================== File Selection ====================

function toggleFileSelection(path) {
    if (selectedFiles.has(path)) {
        selectedFiles.delete(path);
    } else {
        selectedFiles.add(path);
    }
    updateSelectedCount();
}

function selectAll() {
    allFiles.forEach(file => {
        if (file.type !== 'folder') {
            selectedFiles.add(file.path);
        }
    });
    renderFiles(allFiles);
    updateSelectedCount();
    showNotification('✓ All files selected');
}

function deselectAll() {
    selectedFiles.clear();
    renderFiles(allFiles);
    updateSelectedCount();
    showNotification('Cleared selection');
}

function updateSelectedCount() {
    document.getElementById('selectedCount').textContent = selectedFiles.size;
    
    // Disable action buttons if nothing selected
    const hasSelection = selectedFiles.size > 0;
    document.getElementById('deleteBtn').disabled = !hasSelection;
    document.getElementById('archiveBtn').disabled = !hasSelection;
    document.getElementById('uploadBtn').disabled = false; // Always enable
}

// ==================== Search ====================

function toggleSearch() {
    const searchBar = document.getElementById('searchBar');
    searchBar.classList.toggle('hidden');
    
    if (!searchBar.classList.contains('hidden')) {
        document.getElementById('searchInput').focus();
    }
}

function performSearch() {
    const query = document.getElementById('searchInput').value;
    const type = document.getElementById('filterType').value;
    
    if (!query && !type) {
        showNotification('⚠️ Enter a search query');
        return;
    }
    
    const url = `/api/files/search?q=${encodeURIComponent(query)}&type=${encodeURIComponent(type)}`;
    
    fetch(url, {
        headers: {
            'X-Init-Data': tg?.initData || ''
        }
    })
        .then(r => r.json())
        .then(data => {
            allFiles = data.results || [];
            
            if (allFiles.length === 0) {
                document.getElementById('fileBrowser').innerHTML = '';
                document.getElementById('emptyState').classList.remove('hidden');
            } else {
                document.getElementById('emptyState').classList.add('hidden');
                renderFiles(allFiles);
            }
            
            updateBreadcrumb('🔍 Search Results');
            showNotification(`✓ Found ${allFiles.length} result(s)`);
        })
        .catch(err => {
            console.error('Search error:', err);
            showNotification('❌ Search failed');
        });
}

// ==================== Delete Operations ====================

function showDeleteConfirm() {
    if (selectedFiles.size === 0) {
        showNotification('⚠️ No files selected');
        return;
    }
    
    document.getElementById('deleteCount').textContent = `Are you sure you want to delete ${selectedFiles.size} file(s)? This action cannot be undone.`;
    openModal('deleteModal');
}

function confirmDelete() {
    closeModal('deleteModal');
    deleteFiles(Array.from(selectedFiles));
}

function deleteFiles(paths) {
    if (paths.length === 0) return;
    
    showNotification('🗑️ Deleting files...');
    
    fetch('/api/files/delete', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-Init-Data': tg?.initData || ''
        },
        body: JSON.stringify({ paths })
    })
        .then(r => r.json())
        .then(data => {
            showNotification(`✓ ${data.message}`);
            selectedFiles.clear();
            loadFiles();
        })
        .catch(err => {
            console.error('Delete error:', err);
            showNotification('❌ Delete failed');
        });
}

// ==================== Archive Operations ====================

function showArchiveDialog() {
    if (selectedFiles.size === 0) {
        showNotification('⚠️ No files selected');
        return;
    }
    
    document.getElementById('archiveFileCount').textContent = selectedFiles.size;
    document.getElementById('archiveName').value = `archive_${new Date().toISOString().slice(0, 10)}`;
    openModal('archiveModal');
}

function createArchive() {
    const name = document.getElementById('archiveName').value.trim();
    const format = document.querySelector('input[name="archiveFormat"]:checked').value;
    
    if (!name) {
        showNotification('⚠️ Enter archive name');
        return;
    }
    
    closeModal('archiveModal');
    showNotification('📦 Creating archive...');
    
    fetch('/api/files/create-archive', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-Init-Data': tg?.initData || ''
        },
        body: JSON.stringify({
            paths: Array.from(selectedFiles),
            name,
            format
        })
    })
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                showNotification(`❌ Error: ${data.error}`);
            } else {
                showNotification(`✓ Archive created: ${data.archive}`);
                selectedFiles.clear();
                loadFiles();
            }
        })
        .catch(err => {
            console.error('Archive error:', err);
            showNotification('❌ Archive creation failed');
        });
}

// ==================== Upload Dialog ====================

function showUploadDialog() {
    document.getElementById('uploadUrl').value = '';
    openModal('uploadModal');
}

function startUpload() {
    const urls = document.getElementById('uploadUrl').value.trim().split('\n').filter(u => u.trim());
    
    if (urls.length === 0) {
        showNotification('⚠️ Enter at least one URL');
        return;
    }
    
    closeModal('uploadModal');
    showNotification(`📌 Queued ${urls.length} download(s)`);
    
    setTimeout(() => {
        loadFiles();
    }, 2000);
}

// ==================== View Mode ====================

function toggleViewMode() {
    viewMode = viewMode === 'grid' ? 'list' : 'grid';
    
    const browser = document.getElementById('fileBrowser');
    if (viewMode === 'list') {
        browser.classList.add('list-view');
        document.getElementById('viewToggleBtn').innerHTML = '<i class="fas fa-list"></i>';
    } else {
        browser.classList.remove('list-view');
        document.getElementById('viewToggleBtn').innerHTML = '<i class="fas fa-th"></i>';
    }
    
    renderFiles(allFiles);
}

function reloadFiles() {
    selectedFiles.clear();
    loadFiles();
}

// ==================== Modal Management ====================

function openModal(id) {
    const modal = document.getElementById(id);
    if (modal) {
        modal.classList.remove('hidden');
        document.body.style.overflow = 'hidden';
    }
}

function closeModal(id) {
    const modal = document.getElementById(id);
    if (modal) {
        modal.classList.add('hidden');
        document.body.style.overflow = '';
    }
}

// Close modal on outside click
document.addEventListener('click', (e) => {
    if (e.target.classList.contains('modal-overlay')) {
        e.target.parentElement.classList.add('hidden');
        document.body.style.overflow = '';
    }
});

// ==================== Notifications ====================

function showNotification(message, duration = 3000) {
    const notification = document.getElementById('notification');
    document.getElementById('notificationText').textContent = message;
    notification.classList.remove('hidden');
    
    if (duration > 0) {
        setTimeout(hideNotification, duration);
    }
}

function hideNotification() {
    const notification = document.getElementById('notification');
    notification.classList.add('hidden');
}

// ==================== Stats ====================

function loadStats() {
    fetch('/api/stats', {
        headers: {
            'X-Init-Data': tg?.initData || ''
        }
    })
        .then(r => r.json())
        .then(data => {
            // Update stats bar
            document.getElementById('fileCount').textContent = data.file_count;
            document.getElementById('totalSize').textContent = data.total_size;
        })
        .catch(err => console.error('Stats error:', err));
}

// Update stats every 30 seconds
setInterval(loadStats, 30000);

// ==================== Telegram Mini-App Integration ====================

// Send data back to bot if needed
function sendDataToBot(data) {
    if (tg) {
        tg.sendData(JSON.stringify(data));
    }
}

// Handle closing
function closeApp() {
    if (tg) {
        tg.close();
    }
}
