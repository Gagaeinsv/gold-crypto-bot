// Helper script for Admin Panel UI

// Show/Hide Modals
function openModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.remove('hidden');
        modal.classList.add('flex');
    }
}

function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.add('hidden');
        modal.classList.remove('flex');
    }
}

// User-friendly Notifications
function showNotification(message, type = 'info') {
    const container = document.getElementById('notification-container');
    if (!container) return;

    const alert = document.createElement('div');
    alert.className = `p-4 mb-4 text-sm rounded-lg glass-card flex items-center justify-between border animate-slide-in shadow-lg ${
        type === 'success' 
            ? 'text-emerald-400 border-emerald-500/30 bg-emerald-500/10' 
            : type === 'danger'
            ? 'text-rose-400 border-rose-500/30 bg-rose-500/10'
            : 'text-amber-400 border-amber-500/30 bg-amber-500/10'
    }`;
    alert.innerHTML = `
        <div class="flex items-center gap-2">
            <span>${message}</span>
        </div>
        <button onclick="this.parentElement.remove()" class="text-slate-400 hover:text-slate-200 transition-colors ml-4">&times;</button>
    `;

    container.appendChild(alert);
    setTimeout(() => {
        alert.remove();
    }, 4500);
}

// Chart.js helper
function createLineChart(ctxId, label, labels, dataPoints, borderColor = '#3b82f6') {
    const ctx = document.getElementById(ctxId);
    if (!ctx) return;

    new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: label,
                data: dataPoints,
                borderColor: borderColor,
                backgroundColor: borderColor + '1a', // 10% opacity
                borderWidth: 2,
                fill: true,
                tension: 0.3
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    display: false
                }
            },
            scales: {
                y: {
                    grid: {
                        color: 'rgba(255, 255, 255, 0.05)'
                    },
                    ticks: {
                        color: '#94a3b8'
                    }
                },
                x: {
                    grid: {
                        color: 'rgba(255, 255, 255, 0.05)'
                    },
                    ticks: {
                        color: '#94a3b8'
                    }
                }
            }
        }
    });
}
