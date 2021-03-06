#
# Copyright (c) 2019 ISP RAS (http://www.ispras.ru)
# Ivannikov Institute for System Programming of the Russian Academy of Sciences
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from django.urls import path, include

from rest_framework.authtoken.views import obtain_auth_token
from rest_framework.routers import DefaultRouter

from service import views, api

router = DefaultRouter()
router.register('tasks', api.TaskAPIViewset, 'tasks')

urlpatterns = [
    path('', include(router.urls)),
    path('get_token/', obtain_auth_token),
    path('tasks/<int:pk>/download/', api.DownloadTaskArchiveView.as_view()),

    path('solution/', api.SolutionCreateView.as_view()),
    path('solution/<int:task_id>/', api.SolutionDetailView.as_view()),
    path('solution/<int:task_id>/download/', api.SolutionDownloadView.as_view()),

    path('decision-status/<uuid:identifier>/', api.DecisionStatusAPIView.as_view()),

    path('scheduler-user/', api.AddSchedulerUserView.as_view(), name='api-scheduler-user'),
    path('scheduler-user/<uuid:decision_uuid>/', api.SchedulerUserView.as_view()),

    path('progress/<uuid:identifier>/', api.DecisionProgressAPIView.as_view()),
    path('configuration/<uuid:identifier>/', api.DecisionConfigurationAPIView.as_view()),

    path('update-tools/', api.UpdateToolsAPIView.as_view()),
    path('update-nodes/', api.UpdateNodes.as_view()),

    path('scheduler/<slug:type>/', api.SchedulerAPIView.as_view()),
    path('schedulers/', views.SchedulersInfoView.as_view(), name='schedulers'),
]
