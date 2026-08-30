<template>
  <div class="loading-state" aria-label="正在加载" aria-busy="true">
    <span v-for="index in 5" :key="index" class="loading-line" />
  </div>
</template>
