/* ===== FriendStudio App Shell ===== */
(function() {
    'use strict';

    var menuToggle = document.getElementById('sidebar-toggle');
    var sidebar = document.getElementById('sidebar');
    var overlay = document.getElementById('sidebar-overlay');

    function toggleSidebar() {
        sidebar.classList.toggle('open');
        if (overlay) overlay.classList.toggle('open');
    }
    function closeSidebar() {
        sidebar.classList.remove('open');
        if (overlay) overlay.classList.remove('open');
    }

    if (menuToggle) menuToggle.addEventListener('click', toggleSidebar);
    if (overlay) overlay.addEventListener('click', closeSidebar);

    // Active nav item - highlight current page
    var navItems = document.querySelectorAll('.sidebar-item');
    var currentPath = window.location.pathname;
    navItems.forEach(function(item) {
        var href = item.getAttribute('href');
        if (href && (currentPath === href || currentPath.startsWith(href + '/') || currentPath.startsWith(href + '?'))) {
            item.classList.add('active');
        }
        // Also if we're on /app/skills/keyword-research, highlight Keyword Research
        if (currentPath === '/app/skills/keyword-research' && href === '/app/skills/keyword-research') {
            item.classList.add('active');
        }
    });

    // Coming soon tooltips
    var comingSoonItems = document.querySelectorAll('.sidebar-item .coming-soon');
    comingSoonItems.forEach(function(el) {
        var parent = el.closest('.sidebar-item');
        if (parent) {
            parent.addEventListener('click', function(e) {
                e.preventDefault();
            });
        }
    });
})();
