import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import ProbeTrialView from '@/views/ProbeTrialView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/probe-trial', name: 'probeTrial', component: ProbeTrialView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
