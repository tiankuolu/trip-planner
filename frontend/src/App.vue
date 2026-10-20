<template>
  <div id="app">
    <a-layout style="min-height: 100vh">
      <a-layout-header class="app-header">
        <router-link to="/" class="brand">🌍 智能旅行助手</router-link>
        <a-space :size="20">
          <router-link to="/tasks" class="nav-link">任务列表</router-link>
        </a-space>
      </a-layout-header>
      <a-layout-content style="padding: 24px">
        <router-view />
      </a-layout-content>
      <a-layout-footer style="text-align: center">
        智能旅行助手 ©2025
      </a-layout-footer>
    </a-layout>
  </div>
</template>

<script setup lang="ts">
</script>

<style>
#app {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial,
    'Noto Sans', sans-serif;
}

.app-header {
  background: #001529;
  padding: 0 50px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.brand {
  color: #fff;
  font-size: 24px;
  font-weight: bold;
  text-decoration: none;
}

.nav-link {
  color: rgba(255, 255, 255, 0.75);
  font-size: 15px;
  text-decoration: none;
}

.nav-link:hover {
  color: #fff;
}
</style>
